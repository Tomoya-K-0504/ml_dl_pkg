from __future__ import print_function, division

from collections import OrderedDict

import torch
import torch.nn as nn
import torch.nn.functional as F

from ml.models.cnn import cnn_16_751_751

supported_rnns = {
    'lstm': nn.LSTM,
    'rnn': nn.RNN,
    'gru': nn.GRU
}
supported_rnns_inv = dict((v, k) for k, v in supported_rnns.items())


def rnn_args(parser):
    rnn_parser = parser.add_argument_group("RNN model arguments")

    # RNN params
    rnn_parser.add_argument('--rnn-type', default='gru',
                           help='Type of the RNN. rnn|gru|lstm|deepspeech are supported')
    rnn_parser.add_argument('--rnn-hidden-size', default=400, type=int, help='Hidden size of RNNs')
    rnn_parser.add_argument('--rnn-n-layers', default=3, type=int, help='Number of RNN layers')
    rnn_parser.add_argument('--max-norm', default=400, type=int,
                           help='Norm cutoff to prevent explosion of gradients')
    rnn_parser.add_argument('--no-bidirectional', dest='bidirectional', action='store_false', default=True,
                           help='Turn off bi-directional RNNs, introduces lookahead convolution')
    rnn_parser.add_argument('--no-inference-softmax', dest='is_inference_softmax', action='store_false',
                           default=True,
                           help='Turn off inference softmax')
    rnn_parser.add_argument('--no-batch-norm', dest='batch_norm', action='store_false', default=True,
                            help='Batch-wise normalization or not')
    return parser


def construct_rnn(cfg, output_size):
    """

    :param cfg: {
        'rnn_type': 'deepspeech' or lstm or gru,
        'input_size': input feature size of data
        'n_layers': Number of layers in rnn
        'seq_len': Length of time dimension
        'hidden_size': Number of hidden size in rnn
        'is_bidirectional': True or False
        'is_inference_softmax': True or False
    }
    :return:
    """
    rnn_type = cfg['rnn_type']
    if rnn_type == 'deepspeech':
        raise NotImplementedError
        conv, conv_out_ftrs = cnn_16_751_751(output_size)
        return DeepSpeech(conv, conv_out_ftrs, cfg['batch_size'], rnn_type=supported_rnns[rnn_type], labels="abc",
                          eeg_conf=None, rnn_hidden_size=cfg['rnn_hidden_size'], n_layers=cfg['rnn_n_layers'],
                          bidirectional=cfg['bidirectional'],
                          is_inference_softmax=cfg.get('is_inference_softmax', True))
    else:
        return RNNClassifier(cfg['batch_size'], cfg['input_size'], seq_len=cfg['seq_len'], rnn_type=supported_rnns[rnn_type],
                             output_size=output_size, rnn_hidden_size=cfg['rnn_hidden_size'],
                             n_layers=cfg['rnn_n_layers'], bidirectional=cfg['bidirectional'],
                             is_inference_softmax=cfg.get('is_inference_softmax', True), batch_norm=cfg['batch_norm'])


class SequenceWise(nn.Module):
    def __init__(self, module):
        """
        Collapses input of dim T*N*H to (T*N)*H, and applies to a module.
        Allows handling of variable sequence lengths and minibatch sizes.
        :param module: Module to apply input to.
        """
        super(SequenceWise, self).__init__()
        self.module = module

    def forward(self, x):
        t, n = x.size(0), x.size(1)
        # if sequence-wise, normalize at last dimension, should be n x t direction
        x = x.view(t * n, -1)   # t x n x h -> (t x n) x h
        x = self.module(x)
        x = x.view(t, n, -1)
        return x

    def __repr__(self):
        tmpstr = self.__class__.__name__ + ' (\n'
        tmpstr += self.module.__repr__()
        tmpstr += ')'
        return tmpstr


class BatchRNN(nn.Module):
    def __init__(self, input_size, seq_len, hidden_size, batch_size, rnn_type=nn.LSTM, bidirectional=False, batch_norm=True):
        super(BatchRNN, self).__init__()
        self.input_size = input_size
        self.batch_size = batch_size
        self.hidden_size = hidden_size
        self.bidirectional = bidirectional
        self.batch_norm = SequenceWise(nn.BatchNorm1d(batch_size)) if batch_norm else None
        self.rnn = rnn_type(input_size=input_size, hidden_size=hidden_size,
                                            bidirectional=bidirectional, bias=True)
        self.num_directions = 2 if bidirectional else 1

    def flatten_parameters(self):
        self.rnn.flatten_parameters()

    def forward(self, x):

        if self.batch_norm is not None:
            x = self.batch_norm(x)

        x, _ = self.rnn(x)

        if self.bidirectional:
            x = x.view(x.size(0), x.size(1), 2, -1).sum(dim=2).view(x.size(0), x.size(1), -1)  # (TxNxH*2) -> (TxNxH) by sum
        return x


class InferenceBatchSoftmax(nn.Module):
    def forward(self, input_):
        if not self.training:
            return F.softmax(input_, dim=-1)
        else:
            return input_


class RNNClassifier(nn.Module):
    def __init__(self, batch_size, input_size, seq_len, output_size, rnn_type=nn.LSTM, rnn_hidden_size=768,
                 n_layers=5, bidirectional=True, is_inference_softmax=True, batch_norm=True):
        super(RNNClassifier, self).__init__()

        rnns = []
        rnn = BatchRNN(input_size=input_size, seq_len=seq_len, hidden_size=rnn_hidden_size, batch_size=batch_size,
                       rnn_type=rnn_type, bidirectional=bidirectional, batch_norm=batch_norm)
        rnns.append(('0', rnn))
        for x in range(n_layers - 1):
            rnn = BatchRNN(input_size=rnn_hidden_size, seq_len=seq_len, hidden_size=rnn_hidden_size,
                           batch_size=batch_size, rnn_type=rnn_type, bidirectional=bidirectional, batch_norm=batch_norm)
            rnns.append(('%d' % (x + 1), rnn))
        self.rnns = nn.Sequential(OrderedDict(rnns))

        self.fc = nn.Sequential(
            nn.BatchNorm1d(batch_size),
            nn.Linear(rnn_hidden_size * seq_len, output_size, bias=False)
        )

        self.is_inference_softmax = is_inference_softmax

    def forward(self, x):
        x = x.view(x.size(2), x.size(0), x.size(1))    # batch x freq x time -> # time x batch x freq

        for rnn in self.rnns:
            x = rnn(x)

        x = x.transpose(0, 1).reshape(x.size(1), -1).unsqueeze(dim=0)
        x = self.fc(x).squeeze(dim=0)

        # identity in training mode, softmax in eval model
        if self.is_inference_softmax:
            x = InferenceBatchSoftmax()(x)
        else:
            x = F.softmax(x)

        return x


class DeepSpeech(RNNClassifier):
    def __init__(self, conv, conv_out_ftrs, batch_size, rnn_type=nn.LSTM, labels="abc", eeg_conf=None,
                 rnn_hidden_size=768, n_layers=5, bidirectional=True, is_inference_softmax=True):
        super(DeepSpeech, self).__init__(batch_size, input_size=conv_out_ftrs, rnn_type=nn.LSTM, rnn_hidden_size=768,
                                         n_layers=5, bidirectional=True, is_inference_softmax=is_inference_softmax)

        # model metadata needed for serialization/deserialization
        if eeg_conf is None:
            eeg_conf = {}
        self.version = '0.0.1'
        self.hidden_size = rnn_hidden_size
        self.hidden_layers = n_layers
        self.rnn_type = rnn_type
        self.eeg_conf = eeg_conf or {}
        self.labels = labels
        self.bidirectional = bidirectional

        self.conv = conv

    def forward(self, x):

        x = self.conv(x)

        sizes = x.size()
        x = x.view(sizes[0], sizes[1] * sizes[2], sizes[3])  # Collapse feature dimension
        x = x.transpose(1, 2).transpose(0, 1).contiguous()  # TxNxH

        x = super().forward(x)
        return x

    @classmethod
    def load_model(cls, path):
        package = torch.load(path, map_location=lambda storage, loc: storage)
        model = cls(rnn_hidden_size=package['hidden_size'], n_layers=package['hidden_layers'],
                    labels=package['labels'], eeg_conf=package['eeg_conf'],
                    rnn_type=supported_rnns[package['rnn_type']], bidirectional=package.get('bidirectional', True))
        model.load_state_dict(package['state_dict'])
        for x in model.rnns:
            x.flatten_parameters()
        return model

    @classmethod
    def load_model_package(cls, package):
        model = cls(rnn_hidden_size=package['hidden_size'], n_layers=package['hidden_layers'],
                    labels=package['labels'], eeg_conf=package['eeg_conf'],
                    rnn_type=supported_rnns[package['rnn_type']], bidirectional=package.get('bidirectional', True))
        model.load_state_dict(package['state_dict'])
        return model

    @staticmethod
    def serialize(model, optimizer=None, epoch=None, iteration=None, loss_results=None,
                  cer_results=None, wer_results=None, avg_loss=None, meta=None):
        package = {
            'version': model.version,
            'hidden_size': model.hidden_size,
            'hidden_layers': model.hidden_layers,
            'rnn_type': supported_rnns_inv.get(model.rnn_type, model.rnn_type.__name__.lower()),
            'eeg_conf': model.eeg_conf,
            'labels': model.labels,
            'state_dict': model.state_dict(),
            'bidirectional': model.bidirectional
        }
        if optimizer is not None:
            package['optim_dict'] = optimizer.state_dict()
        if avg_loss is not None:
            package['avg_loss'] = avg_loss
        if epoch is not None:
            package['epoch'] = epoch + 1  # increment for readability
        if iteration is not None:
            package['iteration'] = iteration
        if loss_results is not None:
            package['loss_results'] = loss_results
            package['cer_results'] = cer_results
            package['wer_results'] = wer_results
        if meta is not None:
            package['meta'] = meta
        return package

    @staticmethod
    def get_param_size(model):
        params = 0
        for p in model.parameters():
            tmp = 1
            for x in p.size():
                tmp *= x
            params += tmp
        return params