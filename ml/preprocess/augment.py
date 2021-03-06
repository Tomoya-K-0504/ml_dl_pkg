import torch
import torch.nn as nn


def spec_augment_args(parser):
    spec_augment_parser = parser.add_argument_group("Spec augment arguments")
    spec_augment_parser.add_argument('--time-drop-rate', type=float, default=0.0)
    spec_augment_parser.add_argument('--freq-drop-rate', type=float, default=0.0)

    return parser


from dataclasses import dataclass
@dataclass
class SpecAugConfig:
    time_drop_rate: float = 0.0
    freq_drop_rate: float = 0.0


class DropStripes(nn.Module):
    def __init__(self, dim, drop_rate, stripes_num):
        """Drop stripes.

        Args:
          dim: int, dimension along which to drop
          drop_width: int, maximum width of stripes to drop
          stripes_num: int, how many stripes to drop
        """
        super(DropStripes, self).__init__()

        assert dim in [2, 3]    # dim 2: time; dim 3: frequency

        self.dim = dim
        self.drop_rate = drop_rate
        self.stripes_num = stripes_num

    def forward(self, input):
        """input: (batch_size, channels, time_steps, freq_bins)"""

        assert input.ndimension() == 4

        if self.training is False or self.drop_rate == 0.0:
            return input

        else:
            batch_size = input.shape[0]
            total_width = input.shape[self.dim]

            for n in range(batch_size):
                self.transform_slice(input[n], total_width)

            return input

    def transform_slice(self, e, total_width):
        """e: (channels, time_steps, freq_bins)"""

        for _ in range(self.stripes_num):
            distance = torch.randint(low=0, high=int(total_width * self.drop_rate), size=(1,))[0]
            bgn = torch.randint(low=0, high=total_width - distance, size=(1,))[0]

            if self.dim == 2:
                e[:, bgn : bgn + distance, :] = 0
            elif self.dim == 3:
                e[:, :, bgn : bgn + distance] = 0


class SpecAugment:
    def __init__(self, time_drop_rate, freq_drop_rate, time_stripes_num=1, freq_stripes_num=1):
        """Spec augmetation.
        [ref] Park, D.S., Chan, W., Zhang, Y., Chiu, C.C., Zoph, B., Cubuk, E.D.
        and Le, Q.V., 2019. Specaugment: A simple data augmentation method
        for automatic speech recognition. arXiv preprint arXiv:1904.08779.

        Args:
          time_drop_width: int
          time_stripes_num: int
          freq_drop_width: int
          freq_stripes_num: int
        """
        self.time_dropper = DropStripes(dim=2, drop_rate=time_drop_rate,
            stripes_num=time_stripes_num)

        self.freq_dropper = DropStripes(dim=3, drop_rate=freq_drop_rate,
            stripes_num=freq_stripes_num)

    def __call__(self, spect):
        y = self.time_dropper(spect)
        y = self.freq_dropper(y)
        return y
