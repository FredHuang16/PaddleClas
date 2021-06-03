# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import os
import sys
import numpy as np
__dir__ = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.abspath(os.path.join(__dir__, '../../')))

import argparse
import paddle
import paddle.nn as nn
import paddle.distributed as dist

from ppcls.utils.check import check_gpu
from ppcls.utils.misc import AverageMeter
from ppcls.utils import logger
from ppcls.data import build_dataloader
from ppcls.arch import build_model
from ppcls.losses import build_loss
from ppcls.arch.loss_metrics import build_metrics
from ppcls.optimizer import build_optimizer
from ppcls.utils.save_load import load_dygraph_pretrain
from ppcls.utils.save_load import init_model
from ppcls.utils import save_load

from ppcls.data.utils.get_image_list import get_image_list
from ppcls.data.postprocess import build_postprocess
from ppcls.data.reader import create_operators


class Trainer(object):
    def __init__(self, config, mode="train"):
        self.mode = mode
        self.config = config
        self.output_dir = self.config['Global']['output_dir']
        # set device
        assert self.config["Global"]["device"] in ["cpu", "gpu", "xpu"]
        self.device = paddle.set_device(self.config["Global"]["device"])
        # set dist
        self.config["Global"][
            "distributed"] = paddle.distributed.get_world_size() != 1
        if self.config["Global"]["distributed"]:
            dist.init_parallel_env()

        if "Head" in self.config["Arch"]:
            self.config["Arch"]["Head"]["class_num"] = self.config["Global"]["class_num"]
        self.model = build_model(self.config["Arch"])

        if self.config["Global"]["pretrained_model"] is not None:
            load_dygraph_pretrain(self.model,
                                  self.config["Global"]["pretrained_model"])

        if self.config["Global"]["distributed"]:
            self.model = paddle.DataParallel(self.model)

        self.vdl_writer = None
        if self.config['Global']['use_visualdl']:
            from visualdl import LogWriter
            vdl_writer_path = os.path.join(self.output_dir, "vdl")
            if not os.path.exists(vdl_writer_path):
                os.makedirs(vdl_writer_path)
            self.vdl_writer = LogWriter(logdir=vdl_writer_path)
        logger.info('train with paddle {} and device {}'.format(
            paddle.__version__, self.device))

    def _build_metric_info(self, metric_config, mode="train"):
        """
        _build_metric_info: build metrics according to current mode
        Return:
            metric: dict of the metrics info
        """
        metric = None
        mode = mode.capitalize()
        if mode in metric_config and metric_config[mode] is not None:
            metric = build_metrics(metric_config[mode])
        return metric

    def _build_loss_info(self, loss_config, mode="train"):
        """
        _build_loss_info: build loss according to current mode
        Return:
            loss_dict: dict of the loss info
        """
        loss = None
        mode = mode.capitalize()
        if mode in loss_config and loss_config[mode] is not None:
            loss = build_loss(loss_config[mode])
        return loss

    def train(self):
        # build train loss and metric info
        loss_func = self._build_loss_info(self.config["Loss"])

        metric_func = self._build_metric_info(self.config["Metric"])

        train_dataloader = build_dataloader(self.config["DataLoader"], "Train",
                                            self.device)

        step_each_epoch = len(train_dataloader)

        optimizer, lr_sch = build_optimizer(self.config["Optimizer"],
                                            self.config["Global"]["epochs"],
                                            step_each_epoch,
                                            self.model.parameters())

        print_batch_step = self.config['Global']['print_batch_step']
        save_interval = self.config["Global"]["save_interval"]

        best_metric = {
            "metric": 0.0,
            "epoch": 0,
        }
        # key: 
        # val: metrics list word
        output_info = dict()
        # global iter counter
        global_step = 0

        if self.config["Global"]["checkpoints"] is not None:
            metric_info = init_model(self.config["Global"], self.model,
                                     optimizer)
            if metric_info is not None:
                best_metric.update(metric_info)

        for epoch_id in range(best_metric["epoch"] + 1,
                              self.config["Global"]["epochs"] + 1):
            acc = self.eval(0)
            acc = 0.0
            self.model.train()
            for iter_id, batch in enumerate(train_dataloader()):
                batch_size = batch[0].shape[0]
                batch[1] = paddle.to_tensor(batch[1].numpy().astype("int64")
                                            .reshape([-1, 1]))
                global_step += 1
                # image input
                out = self.model(batch[0], batch[1])
                # calc loss
                loss_dict = loss_func(out, batch[-1])
                for key in loss_dict:
                    if not key in output_info:
                        output_info[key] = AverageMeter(key, '7.5f')
                    output_info[key].update(loss_dict[key].numpy()[0],
                                            batch_size)
                # calc metric
                if metric_func is not None:
                    metric_dict = metric_func(out, batch[-1])
                    for key in metric_dict:
                        if not key in output_info:
                            output_info[key] = AverageMeter(key, '7.5f')
                        output_info[key].update(metric_dict[key].numpy()[0],
                                                batch_size)

                if iter_id % print_batch_step == 0:
                    lr_msg = "lr: {:.5f}".format(lr_sch.get_lr())
                    metric_msg = ", ".join([
                        "{}: {:.5f}".format(key, output_info[key].avg)
                        for key in output_info
                    ])
                    logger.info("[Train][Epoch {}][Iter: {}/{}]{}, {}".format(
                        epoch_id, iter_id,
                        len(train_dataloader), lr_msg, metric_msg))

                # step opt and lr
                loss_dict["loss"].backward()
                optimizer.step()
                optimizer.clear_grad()
                lr_sch.step()

            metric_msg = ", ".join([
                "{}: {:.5f}".format(key, output_info[key].avg)
                for key in output_info
            ])
            logger.info("[Train][Epoch {}][Avg]{}".format(epoch_id,
                                                          metric_msg))
            output_info.clear()

            # eval model and save model if possible
            if self.config["Global"][
                    "eval_during_train"] and epoch_id % self.config["Global"][
                        "eval_during_train"] == 0:
                acc = self.eval(epoch_id)
                if acc > best_metric["metric"]:
                    best_metric["metric"] = acc
                    best_metric["epoch"] = epoch_id
                    save_load.save_model(
                        self.model,
                        optimizer,
                        best_metric,
                        self.output_dir,
                        model_name=self.config["Arch"]["name"],
                        prefix="best_model")

            # save model
            if epoch_id % save_interval == 0:
                save_load.save_model(
                    self.model,
                    optimizer, {"metric": acc,
                                "epoch": epoch_id},
                    self.output_dir,
                    model_name=self.config["Arch"]["name"],
                    prefix="ppcls_epoch_{}".format(epoch_id))

    def build_avg_metrics(self, info_dict):
        return {key: AverageMeter(key, '7.5f') for key in info_dict}

    @paddle.no_grad()
    def eval(self, epoch_id=0):
        output_info = dict()


        query_dataloader = build_dataloader(self.config["DataLoader"], "Eval",
                                           self.device)
        gallery_dataloader = build_dataloader(self.config["DataLoader"], "Test",
                                           self.device)        
        self.model.eval()
        print_batch_step = self.config["Global"]["print_batch_step"]

        # build train loss and metric info
        # step1. build gallery
        all_feas = None
        all_labs = None
        query_feas = None
        query_labs = None
        
        for idx, batch in enumerate(gallery_dataloader()):  # load is very time-consuming
            batch = [paddle.to_tensor(x) for x in batch]
            batch[1] = batch[1].reshape([-1, 1])
            out = self.model(batch[0], batch[1])
            batch_feas = out["features"]

            # do norm
            feas_norm = paddle.sqrt(
                paddle.sum(paddle.square(batch_feas), axis=1, keepdim=True))
            batch_feas = paddle.divide(batch_feas, feas_norm)

            # to cpu place
            batch_feas = batch_feas.cpu()
            batch_labels = batch[1].cpu()

            if all_feas is None:
                all_feas = batch_feas
            else:
                all_feas = paddle.concat([all_feas, batch_feas])

            if all_labs is None:
                all_labs = batch_labels
            else:
                all_labs = paddle.concat([all_labs, batch_labels])

            if idx % print_batch_step == 0:
                logger.info("Eval step: [{}/{}]".format(
                    idx, len(gallery_dataloader)))

        logger.info("Build gallery done, all feat shape: {}, begin to eval..".
                    format(all_feas.shape))
        
        for idx, batch in enumerate(query_dataloader()):  # load is very time-consuming
            batch = [paddle.to_tensor(x) for x in batch]
            batch[1] = batch[1].reshape([-1, 1])
            out = self.model(batch[0], batch[1])
            batch_feas = out["features"]
            # do norm
            feas_norm = paddle.sqrt(
                paddle.sum(paddle.square(batch_feas), axis=1, keepdim=True))
            batch_feas = paddle.divide(batch_feas, feas_norm)

            # to cpu place
            batch_feas = batch_feas.cpu()
            batch_labels = batch[1].cpu()
            #print(batch_feas.shape)
            if query_feas is None:
                query_feas = batch_feas
            else:
                query_feas = paddle.concat([query_feas, batch_feas])

            if query_labs is None:
                query_labs = batch_labels
            else:
                query_labs = paddle.concat([query_labs, batch_labels])
            #print(query_feas.shape)
            if idx % print_batch_step == 0:
                logger.info("Eval step: [{}/{}]".format(
                    idx, len(query_dataloader)))

        logger.info("Build query done, all feat shape: {}".format(query_feas.shape))

        # step2. do evaluation
        fea_blocks = paddle.split(
            query_feas, num_or_sections=self.config["Global"]["num_split"])
        choosen_label = None

        for block_fea in fea_blocks:
            similarities_matrix = paddle.matmul(
                block_fea, all_feas, transpose_y=True)
            indicies = paddle.argsort(
                similarities_matrix, axis=1, descending=True)
            indicies = indicies[:, 0]
            choosen_label_tmp = paddle.gather(all_labs, indicies)
            if choosen_label is None:
                choosen_label = choosen_label_tmp
            else:
                choosen_label = paddle.concat(
                    [choosen_label, choosen_label_tmp])

        result = paddle.cast(paddle.equal(choosen_label, query_labs), 'float32')
        recall = paddle.mean(result).numpy()[0]
        logger.info("Eval done, recall: {:.5f}".format(recall))
        self.model.train()

        return recall
