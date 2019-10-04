import random
import time
from abc import ABCMeta
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import torch
from sklearn.metrics import confusion_matrix
from tensorboardX import SummaryWriter
from tqdm import tqdm

from ml.models.base_model import model_args
from ml.models.ml_model import MLModel
from ml.models.nn_model import NNModel, supported_nn_models


def model_manager_args(parser):

    model_manager_parser = parser.add_argument_group("Model manager arguments")
    model_manager_parser.add_argument('--train-path', help='data file for training', default='input/train.csv')
    model_manager_parser.add_argument('--val-path', help='data file for validation', default='input/val.csv')
    model_manager_parser.add_argument('--test-path', help='data file for testing', default='input/test.csv')

    model_manager_parser.add_argument('--model-type', default='rnn',
                                      choices=['rnn', 'cnn', 'cnn_rnn', 'xgboost', 'knn', 'catboost', 'sgdc'])
    model_manager_parser.add_argument('--gpu-id', default=0, type=int, help='ID of GPU to use')

    # optimizer params
    optim_param_parser = parser.add_argument_group("Optimizer parameter arguments for learning")
    optim_param_parser.add_argument('--optimizer', default='adam', help='Type of optimizer. ',
                                    choices=['sgd', 'adam', 'rmsprop'])
    optim_param_parser.add_argument('--lr', '--learning-rate', default=1e-3, type=float, help='initial learning rate')
    optim_param_parser.add_argument('--momentum', default=0.9, type=float, help='momentum')
    optim_param_parser.add_argument('--weight-decay', default=0.0, type=float, help='weight-decay')
    optim_param_parser.add_argument('--learning-anneal', default=1.1, type=float,
                                    help='Annealing applied to learning rate every epoch')

    hyper_param_parser = parser.add_argument_group("Hyper parameter arguments for learning")
    hyper_param_parser.add_argument('--batch-size', default=32, type=int, help='Batch size for training')
    hyper_param_parser.add_argument('--epoch-rate', default=1.0, type=float, help='Data rate to to use in one epoch')
    hyper_param_parser.add_argument('--n-jobs', default=4, type=int, help='Number of workers used in data-loading')
    type_float_list = lambda x: list(map(float, x.split(',')))
    hyper_param_parser.add_argument('--loss-weight', default='1.0,1.0,1.0', type=type_float_list,
                                    help='The weights of all class about loss')
    hyper_param_parser.add_argument('--sample-balance', default='0.0,0.0,0.0', type=type_float_list,
                                    help='Sampling label balance from dataset.')
    hyper_param_parser.add_argument('--epochs', default=20, type=int, help='Number of training epochs')

    # General parameters for training
    general_param_parser = parser.add_argument_group("General parameters for training")
    general_param_parser.add_argument('--model-path', help='Model file to load model',
                                      default='ml/outputs/models/sth.pth')
    general_param_parser.add_argument('--task-type', help='Task type. regress or classify',
                                      default='classify', choices=['classify', 'regress'])
    general_param_parser.add_argument('--seed', default=0, type=int, help='Seed to generators')
    general_param_parser.add_argument('--cuda', dest='cuda', action='store_true', help='Use cuda to train model')

    # Logging of criterion
    logging_parser = parser.add_argument_group("Logging parameters")
    logging_parser.add_argument('--silent', dest='silent', action='store_true', help='Turn off progress tracking per iteration')
    logging_parser.add_argument('--log-id', default='results', help='Identifier for tensorboard run')
    logging_parser.add_argument('--tensorboard', dest='tensorboard', action='store_true', help='Turn on tensorboard graphing')
    logging_parser.add_argument('--log-dir', default='visualize/', help='Location of tensorboard log')

    parser = model_args(parser)

    return parser


@contextmanager
def simple_timer(label):
    start = time.time()
    yield
    end = time.time()
    print('{}: {:.3f}'.format(label, end - start))


class BaseModelManager(metaclass=ABCMeta):
    def __init__(self, class_labels, cfg, dataloaders, metrics):
        self.class_labels = class_labels
        self.cfg = cfg
        self.dataloaders = dataloaders
        self.device = self._init_device()
        self.model = self._init_model()
        self._init_seed()
        self.logger = self._init_logger()
        self.metrics = metrics
        Path(self.cfg['model_path']).parent.mkdir(exist_ok=True, parents=True)

    @staticmethod
    def check_keys_from_dict(must_contain_keys, dic):
        for key in must_contain_keys:
            assert key in dic.keys(), f'{key} must be in {str(dic)}'

    def _init_model(self):
        self.cfg['input_size'] = self.dataloaders['train'].get_input_size()

        if self.cfg['model_type'] in ['rnn', 'cnn', 'cnn_rnn']:
            if self.cfg['model_type'] in ['rnn', 'cnn_rnn']:
                self.cfg['batch_norm_size'] = self.dataloaders['train'].get_batch_norm_size()
                self.cfg['seq_len'] = self.dataloaders['train'].get_seq_len()
            else:
                self.cfg['image_size'] = self.dataloaders['train'].get_image_size()
                self.cfg['n_channels'] = self.dataloaders['train'].get_n_channels()

            return NNModel(self.class_labels, self.cfg)

        elif self.cfg['model_type'] in ['xgboost', 'catboost', 'sgdc', 'knn']:
            return MLModel(self.class_labels, self.cfg)
        
    def _init_seed(self):
        # Set seeds for determinism
        torch.manual_seed(self.cfg['seed'])
        torch.cuda.manual_seed_all(self.cfg['seed'])
        np.random.seed(self.cfg['seed'])
        random.seed(self.cfg['seed'])

    def _init_device(self):
        if self.cfg['cuda'] and self.cfg['model_type'] in supported_nn_models:
            device = torch.device("cuda")
            torch.cuda.set_device(self.cfg['gpu_id'])
        else:
            device = torch.device("cpu")

        return device

    def _init_logger(self):
        if 'tensorboard' in self.cfg.keys():
            return TensorBoardLogger(self.cfg['log_id'], self.cfg['log_dir'])

    def _verbose(self, epoch, phase, i):
        print('{0} epoch: [{1}][{2}/{3}]'.format(phase, epoch, i + 1, len(self.dataloaders[phase])), end='\t')
        for metric in self.metrics:
            print('{} {:.4f}'.format(metric.name, metric.average_meter[phase].value), end='\t')
        print('')

    def _record_log(self, phase, epoch):
        values = {}
        for metric in self.metrics:
            values[phase + '_' + metric.name] = metric.average_meter[phase].average
            print(phase + '_' + metric.name, metric.average_meter[phase].average, '\t')
        self.logger.update(epoch, values)

    def _update_by_epoch(self, phase, epoch, learning_anneal):
        for metric in self.metrics:
            best_flag = metric.average_meter[phase].update_best()
            if metric.save_model and best_flag and phase == 'val':
                print("Found better validated model, saving to %s" % self.cfg['model_path'])
                self.model.save_model()

            # reset epoch average meter
            metric.average_meter[phase].reset()

        # anneal lr
        if phase == 'train':
            self.model.anneal_lr(learning_anneal)

        if phase == 'val':
            print(f'epoch {epoch} ended.')

    def _predict(self, phase):
        batch_size = self.cfg['batch_size']

        self.check_keys_from_dict([phase], self.dataloaders)

        dtype_ = np.int if self.cfg['task_type'] == 'classify' else np.float
        # ラベルが入れられなかった部分を除くため、小さな負の数を初期値として格納
        pred_list = np.zeros((len(self.dataloaders[phase]) * batch_size, 1), dtype=dtype_) - 1000000
        label_list = np.zeros((len(self.dataloaders[phase]) * batch_size, 1), dtype=dtype_) - 1000000
        for i, (inputs, labels) in tqdm(enumerate(self.dataloaders[phase]), total=len(self.dataloaders[phase])):
            inputs, labels = inputs.to(self.device), labels.numpy().reshape(-1,)
            preds = self.model.predict(inputs)

            pred_list[i * batch_size:i * batch_size + preds.shape[0], 0] = preds.reshape(-1,)
            label_list[i * batch_size:i * batch_size + labels.shape[0], 0] = labels

        return pred_list[~(pred_list == -1000000)], label_list[~(label_list == -1000000)]

    def train(self):
        self.check_keys_from_dict(['train', 'val'], self.dataloaders)

        for epoch in range(self.cfg['epochs']):
            for phase in ['train', 'val']:

                for i, (inputs, labels) in enumerate(self.dataloaders[phase]):

                    loss, predicts = self.model.fit(inputs.to(self.device), labels.to(self.device), phase)

                    # save loss and metrics in one batch
                    for metric in self.metrics:
                        metric.update(phase, loss, predicts, labels.numpy())

                    if not self.cfg['silent']:
                        self._verbose(epoch, phase, i)

                if self.logger:
                    self._record_log(phase, epoch)

                self._update_by_epoch(phase, epoch, self.cfg['learning_anneal'])

        return self.model

    def test(self, return_metrics=False, load_best=True):
        if load_best:
            self.model.load_model()

        pred_list, label_list = self._predict(phase='test')

        for metric in self.metrics:
            if metric.name == 'loss':
                if self.cfg['task_type'] == 'classify':
                    continue        # lossの計算はモデルによるため、今は未対応
                if self.cfg['model_type'] in ['rnn', 'cnn']:
                    loss_value = self.model.criterion(torch.from_numpy(pred_list),
                                                      torch.from_numpy(label_list))
            else:
                loss_value = 10000000

            metric.update(phase='test', loss_value=loss_value, preds=pred_list, labels=label_list)
            print(f"{metric.name}: {metric.average_meter['test'].value :.4f}")

        if self.cfg['task_type'] == 'classify':
            confusion_matrix_ = confusion_matrix(label_list, pred_list,
                                                 labels=list(range(len(self.class_labels))))
            print(confusion_matrix_)

        if return_metrics:
            return pred_list, label_list, self.metrics
        return pred_list, label_list

    def infer(self, load_best=True):
        if load_best:
            self.model.load_model()

        # test実装
        pred_list, _ = self._predict(phase='infer')
        return pred_list


class TensorBoardLogger(object):
    def __init__(self, id, log_dir):
        Path(log_dir).mkdir(exist_ok=True, parents=True)
        self.id = id
        self.tensorboard_writer = SummaryWriter(log_dir)

    def update(self, epoch, values):
        self.tensorboard_writer.add_scalars(self.id, values, epoch + 1)
