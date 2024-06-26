#!/usr/bin/python
# -*- encoding: utf-8 -*-
from comet_ml import Experiment
from model.model_stages import BiSeNet
from GTA5 import GTA5
import torch
from torch.utils.data import Subset, DataLoader
import logging
import argparse
import numpy as np
from tensorboardX import SummaryWriter
import torch.cuda.amp as amp
from Utils.utils import poly_lr_scheduler
from Utils.utils import (
    reverse_one_hot,
    compute_global_accuracy,
    fast_hist,
    per_class_iu,
)
from tqdm import tqdm
import sys
import os
import Utils.split_GTA5 as split_GTA5
import json


logger = logging.getLogger()

experiment = Experiment(api_key="your-api-key", project_name="AML_project")


def val(args, model, dataloader):
    print("start val!")
    with torch.no_grad():
        model.eval()
        precision_record = []
        hist = np.zeros((args.num_classes, args.num_classes))

        for i, (data, label) in enumerate(tqdm(dataloader)):
            label = label.type(torch.LongTensor)
            data = data.cuda()
            label = label.long().cuda()

            # get RGB predict image
            predict, _, _ = model(data)
            print("Predict", predict)
            print(predict.shape)
            predict = predict.squeeze(0)
            print("Predict after squeeze", predict)
            predict = reverse_one_hot(predict)
            print("Predict after reverse_one_hot", predict)
            predict = np.array(predict.cpu())

            # get RGB label image
            label = label.squeeze()
            label = np.array(label.cpu())

            # compute per pixel accuracy
            precision = compute_global_accuracy(predict, label)
            hist += fast_hist(label.flatten(), predict.flatten(), args.num_classes)

            precision_record.append(precision)

        precision = np.mean(precision_record)
        miou_list = per_class_iu(hist)
        miou = np.mean(miou_list)
        print("precision per pixel for test: %.3f" % precision)
        print("mIoU for validation: %.3f" % miou)
        print(f"mIoU per class: {miou_list}")
        experiment.log_metric("precision", precision)
        experiment.log_metric("miou", miou)

        return precision, miou


def train(args, model, optimizer, dataloader_train, dataloader_val):

    print("start train!")
    writer = SummaryWriter(comment="".format(args.optimizer))

    scaler = amp.GradScaler()

    loss_func = torch.nn.CrossEntropyLoss(ignore_index=255)
    max_miou = 0
    step = 0
    for epoch in range(args.num_epochs):
        lr = poly_lr_scheduler(
            optimizer, args.learning_rate, iter=epoch, max_iter=args.num_epochs
        )
        model.train()
        tq = tqdm(total=len(dataloader_train) * args.batch_size)
        tq.set_description("Current epoch %d, lr %f" % (epoch, lr))
        loss_record = []
        for i, (data, label) in enumerate(dataloader_train):
            data = data.cuda()
            label = label.long().cuda()
            optimizer.zero_grad()

            with amp.autocast():
                output, out16, out32 = model(data)
                loss1 = loss_func(output, label.squeeze(1))
                loss2 = loss_func(out16, label.squeeze(1))
                loss3 = loss_func(out32, label.squeeze(1))
                loss = loss1 + loss2 + loss3

            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()

            tq.update(args.batch_size)
            tq.set_postfix(loss="%.6f" % loss)
            step += 1
            writer.add_scalar("loss_step", loss, step)
            loss_record.append(loss.item())
        tq.close()
        loss_train_mean = np.mean(loss_record)
        writer.add_scalar("epoch/loss_epoch_train", float(loss_train_mean), epoch)
        print("loss for train : %f" % (loss_train_mean))
        if epoch % args.checkpoint_step == 0 and epoch != 0:
            import os

            if not os.path.isdir(args.save_model_path):
                os.mkdir(args.save_model_path)
            torch.save(
                model.module.state_dict(),
                os.path.join(args.save_model_path, "latest.pth"),
            )

        if epoch % args.validation_step == 0 and epoch != 0:
            precision, miou = val(args, model, dataloader_val)
            if miou > max_miou:
                max_miou = miou
                import os

                os.makedirs(args.save_model_path, exist_ok=True)
                torch.save(
                    model.module.state_dict(),
                    os.path.join(args.save_model_path, "best.pth"),
                )
            writer.add_scalar("epoch/precision_val", precision, epoch)
            writer.add_scalar("epoch/miou val", miou, epoch)

    experiment.log_metric("loss_train_mean", loss_train_mean)


def str2bool(v):
    if v.lower() in ("yes", "true", "t", "y", "1"):
        return True
    elif v.lower() in ("no", "false", "f", "n", "0"):
        return False
    else:
        raise argparse.ArgumentTypeError("Unsupported value encountered.")


def parse_args():
    parse = argparse.ArgumentParser()

    parse.add_argument(
        "--mode",
        dest="mode",
        type=str,
        default="train",
    )

    parse.add_argument(
        "--backbone",
        dest="backbone",
        type=str,
        default="STDCNet813",
    )
    parse.add_argument(
        "--pretrain_path",
        dest="pretrain_path",
        type=str,
        default="./checkpoints/STDCNet813M_73.91.tar",
    )
    parse.add_argument(
        "--use_conv_last",
        dest="use_conv_last",
        type=str2bool,
        default=False,
    )
    parse.add_argument(
        "--num_epochs", type=int, default=50, help="Number of epochs to train for"
    )
    parse.add_argument(
        "--epoch_start_i",
        type=int,
        default=0,
        help="Start counting epochs from this number",
    )
    parse.add_argument(
        "--checkpoint_step",
        type=int,
        default=5,
        help="How often to save checkpoints (epochs)",
    )
    parse.add_argument(
        "--validation_step",
        type=int,
        default=5,
        help="How often to perform validation (epochs)",
    )
    parse.add_argument(
        "--crop_height",
        type=int,
        default=512,
        help="Height of cropped/resized input image to modelwork",
    )
    parse.add_argument(
        "--crop_width",
        type=int,
        default=1024,
        help="Width of cropped/resized input image to modelwork",
    )
    parse.add_argument(
        "--batch_size", type=int, default=8, help="Number of images in each batch"
    )
    parse.add_argument(
        "--learning_rate",
        type=float,
        default=0.0001,
        help="learning rate used for train",
    )
    parse.add_argument("--num_workers", type=int, default=2, help="num of workers")
    parse.add_argument(
        "--num_classes", type=int, default=19, help="num of object classes (with void)"
    )
    parse.add_argument(
        "--cuda", type=str, default="0", help="GPU ids used for training"
    )
    parse.add_argument(
        "--use_gpu", type=bool, default=True, help="whether to user gpu for training"
    )
    parse.add_argument(
        "--save_model_path",
        type=str,
        default="./saved_model",
        help="path to save model",
    )
    parse.add_argument(
        "--optimizer",
        type=str,
        default="adam",
        help="optimizer, support rmsprop, sgd, adam",
    )
    parse.add_argument("--loss", type=str, default="crossentropy", help="loss function")
    parse.add_argument(
        "--data_aug", type=str, default="False", help="apply data augmentation or not"
    )

    return parse.parse_args()


def main():
    args = parse_args()
    experiment.log_parameters(vars(args))
    ## dataset
    n_classes = args.num_classes

    data_aug = bool(args.data_aug.lower() == "true")
    root = "./GTA5"

    with open("./Datasets/GTA5_info.json", "r") as fr:
        labels_info = json.load(fr)

    # Check if the dataset is split in train and val
    subdirectories = [
        subdir
        for subdir in os.listdir(root)
        if os.path.isdir(os.path.join(root, subdir))
    ]
    if "train" not in subdirectories or "val" not in subdirectories:
        split_GTA5.main(root)

    train_dataset = GTA5(
        root, labels_info=labels_info, mode="train", apply_transform=data_aug
    )

    dataloader_train = DataLoader(
        train_dataset,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=args.num_workers,
        pin_memory=True,
        drop_last=True,
    )

    val_dataset = GTA5(root, labels_info=labels_info, mode="val", apply_transform=False)
    dataloader_val = DataLoader(
        val_dataset,
        batch_size=1,
        shuffle=False,
        num_workers=args.num_workers,
        drop_last=False,
    )

    ## model
    model = BiSeNet(
        backbone=args.backbone,
        n_classes=n_classes,
        pretrain_model=args.pretrain_path,
        use_conv_last=args.use_conv_last,
    )

    if torch.cuda.is_available() and args.use_gpu:
        model = torch.nn.DataParallel(model).cuda()

    ## optimizer
    # build optimizer
    if args.optimizer == "rmsprop":
        optimizer = torch.optim.RMSprop(model.parameters(), args.learning_rate)
    elif args.optimizer == "sgd":
        optimizer = torch.optim.SGD(
            model.parameters(), args.learning_rate, momentum=0.9, weight_decay=1e-4
        )
    elif args.optimizer == "adam":
        optimizer = torch.optim.Adam(model.parameters(), args.learning_rate)
    else:  # rmsprop
        print("not supported optimizer \n")
        return None

    ## train loop
    train(args, model, optimizer, dataloader_train, dataloader_val)
    # final test
    val(args, model, dataloader_val)
    experiment.end()


if __name__ == "__main__":

    output_file = "output_gta5.txt"
    with open(output_file, "w") as f:

        sys.stdout = f

        main()

        sys.stdout = sys.__stdout__
