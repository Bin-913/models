from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import shutil
import numpy as np
import time
import argparse
import functools

import paddle
import paddle.fluid as fluid
from pyramidbox import PyramidBox
import reader
from utility import add_arguments, print_arguments

parser = argparse.ArgumentParser(description=__doc__)
add_arg = functools.partial(add_arguments, argparser=parser)

# yapf: disable
add_arg('parallel',         bool,  True,            "Whether use multi-GPU/threads or not.")
add_arg('learning_rate',    float, 0.001,           "The start learning rate.")
add_arg('batch_size',       int,   16,              "Minibatch size.")
add_arg('epoc_num',         int,   160,             "Epoch number.")
add_arg('use_gpu',          bool,  True,            "Whether use GPU.")
add_arg('use_pyramidbox',   bool,  True,            "Whether use PyramidBox model.")
add_arg('model_save_dir',   str,   'output',        "The path to save model.")
add_arg('resize_h',         int,   640,             "The resized image height.")
add_arg('resize_w',         int,   640,             "The resized image width.")
add_arg('mean_BGR',         str,   '104., 117., 123.', "Mean value for B,G,R channel which will be subtracted.")
add_arg('with_mem_opt',     bool,  True,            "Whether to use memory optimization or not.")
add_arg('pretrained_model', str,   './vgg_ilsvrc_16_fc_reduced/', "The init model path.")
add_arg('data_dir',         str,   'data',          "The base dir of dataset")
#yapf: enable

train_parameters = {
    "train_images": 12880,
    "image_shape": [3, 640, 640],
    "class_num": 2,
    "batch_size": 16,
    "lr": 0.001,
    "lr_epochs": [99, 124, 149],
    "lr_decay": [1, 0.1, 0.01, 0.001],
    "epoc_num": 160,
    "optimizer_method": "momentum",
    "use_pyramidbox": True
}

def optimizer_setting(train_params):
    batch_size = train_params["batch_size"]
    iters = train_params["train_images"] // batch_size
    lr = train_params["lr"]
    optimizer_method = train_params["optimizer_method"]
    boundaries = [i * iters for i in train_params["lr_epochs"]]
    values = [i * lr for i in train_params["lr_decay"]]

    if optimizer_method == "momentum":
        optimizer = fluid.optimizer.Momentum(
            learning_rate=fluid.layers.piecewise_decay(boundaries, values),
            momentum=0.9,
            regularization=fluid.regularizer.L2Decay(0.0005),
        )
    else:
        optimizer = fluid.optimizer.RMSProp(
            learning_rate=fluid.layers.piecewise_decay(boundaries, values),
            regularization=fluid.regularizer.L2Decay(0.0005),
        )
    return optimizer


def build_program(train_params, main_prog, startup_prog, args):
    use_pyramidbox = train_params["use_pyramidbox"]
    image_shape = train_params["image_shape"]
    class_num = train_params["class_num"]
    with fluid.program_guard(main_prog, startup_prog):
        py_reader = fluid.layers.py_reader(
            capacity=8,
            shapes=[[-1] + image_shape, [-1, 4], [-1, 4], [-1, 1]],
            lod_levels=[0, 1, 1, 1],
            dtypes=["float32", "float32", "float32", "int32"],
            use_double_buffer=True)
        with fluid.unique_name.guard():
            image, face_box, head_box, gt_label = fluid.layers.read_file(py_reader)
            fetches = []
            network = PyramidBox(image=image,
                                 face_box=face_box,
                                 head_box=head_box,
                                 gt_label=gt_label,
                                 sub_network=use_pyramidbox)
            if use_pyramidbox:
                face_loss, head_loss, loss = network.train()
                fetches = [face_loss, head_loss]
            else:
                loss = network.vgg_ssd_loss()
                fetches = [loss]
            optimizer = optimizer_setting(train_params)
            optimizer.minimize(loss)
    return py_reader, fetches, loss

def train(args, config, train_params, train_file_list):
    batch_size = train_params["batch_size"]
    epoc_num = train_params["epoc_num"]
    optimizer_method = train_params["optimizer_method"]
    use_pyramidbox = train_params["use_pyramidbox"]

    use_gpu = args.use_gpu
    model_save_dir = args.model_save_dir
    pretrained_model = args.pretrained_model
    with_memory_optimization = args.with_mem_opt

    devices = os.getenv("CUDA_VISIBLE_DEVICES") or ""
    devices_num = len(devices.split(","))
    batch_size_per_device = batch_size // devices_num
    iters_per_epoc = train_params["train_images"] // batch_size
    num_workers = 8
    is_shuffle = True

    startup_prog = fluid.Program()
    train_prog = fluid.Program()

    train_py_reader, fetches, loss = build_program(
        train_params = train_params,
        main_prog = train_prog,
        startup_prog = startup_prog,
        args=args)

    if with_memory_optimization:
        fluid.memory_optimize(train_prog)

    place = fluid.CUDAPlace(0) if use_gpu else fluid.CPUPlace()
    exe = fluid.Executor(place)
    exe.run(startup_prog)

    start_epoc = 0
    if pretrained_model:
        if pretrained_model.isdigit():
            start_epoc = int(pretrained_model) + 1
            pretrained_model = os.path.join(model_save_dir, pretrained_model)
            print("Resume from %s " %(pretrained_model))

        if not os.path.exists(pretrained_model):
            raise ValueError("The pre-trained model path [%s] does not exist." %
                             (pretrained_model))
        def if_exist(var):
            return os.path.exists(os.path.join(pretrained_model, var.name))
        fluid.io.load_vars(
            exe, pretrained_model, main_program=train_prog, predicate=if_exist)
    train_reader = reader.train(config,
                                train_file_list,
                                batch_size_per_device,
                                shuffle = is_shuffle,
                                use_multiprocessing=True,
                                num_workers = num_workers,
                                max_queue=24)
    train_py_reader.decorate_paddle_reader(train_reader)

    if args.parallel:
        train_exe = fluid.ParallelExecutor(
            main_program = train_prog,
            use_cuda=use_gpu,
            loss_name=loss.name)

    def save_model(postfix, program):
        model_path = os.path.join(model_save_dir, postfix)
        if os.path.isdir(model_path):
            shutil.rmtree(model_path)

        print('save models to %s' % (model_path))
        fluid.io.save_persistables(exe, model_path, main_program=program)

    train_py_reader.start()
    try:
        for pass_id in range(start_epoc, epoc_num):
            start_time = time.time()
            prev_start_time = start_time
            end_time = 0
            batch_id = 0
            for batch_id in range(iters_per_epoc):
                prev_start_time = start_time
                start_time = time.time()
                if args.parallel:
                    fetch_vars = train_exe.run(fetch_list=
                        [v.name for v in fetches])
                else:
                    fetch_vars = exe.run(train_prog,
                                         fetch_list=fetches)
                end_time = time.time()
                fetch_vars = [np.mean(np.array(v)) for v in fetch_vars]
                if batch_id % 10 == 0:
                    if not args.use_pyramidbox:
                        print("Pass {:d}, batch {:d}, loss {:.6f}, time {:.5f}".format(
                            pass_id, batch_id, fetch_vars[0],
                            start_time - prev_start_time))
                    else:
                        print("Pass {:d}, batch {:d}, face loss {:.6f}, " \
                              "head loss {:.6f}, " \
                              "time {:.5f}".format(pass_id,
                               batch_id, fetch_vars[0], fetch_vars[1],
                               start_time - prev_start_time))
            if pass_id % 1 == 0 or pass_id == epoc_num - 1:
                save_model(str(pass_id), train_prog)
    except fluid.core.EOFException:
        train_py_reader.reset()
    except StopIteration:
        train_py_reader.reset()
    train_py_reader.reset()

if __name__ == '__main__':
    args = parser.parse_args()
    print_arguments(args)

    data_dir = os.path.join(args.data_dir, 'WIDER_train/images/')
    train_file_list = os.path.join(args.data_dir,
        'wider_face_split/wider_face_train_bbx_gt.txt')
    mean_BGR = [float(m) for m in args.mean_BGR.split(",")]
    image_shape = [3, int(args.resize_h), int(args.resize_w)]
    train_parameters["image_shape"] = image_shape
    train_parameters["use_pyramidbox"] = args.use_pyramidbox
    train_parameters["batch_size"] = args.batch_size
    train_parameters["lr"] = args.learning_rate
    train_parameters["epoc_num"] = args.epoc_num


    config = reader.Settings(
        data_dir=data_dir,
        resize_h=image_shape[1],
        resize_w=image_shape[2],
        apply_distort=True,
        apply_expand=False,
        mean_value=mean_BGR,
        ap_version='11point')
    train(args, config, train_parameters, train_file_list)
