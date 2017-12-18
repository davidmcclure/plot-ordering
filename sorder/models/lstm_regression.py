

import numpy as np

import os
import click
import torch
import attr
import random
import ujson

from tqdm import tqdm
from itertools import islice
from glob import glob
from boltons.iterutils import pairwise, chunked_iter
from scipy import stats

from torch import nn
from torch.nn.utils.rnn import pack_padded_sequence
from torch.autograd import Variable
from torch.nn import functional as F

from sorder.cuda import ftype, itype
from sorder.vectors import LazyVectors
from sorder.utils import checkpoint, pad_and_pack


vectors = LazyVectors.read()


def read_abstracts(path):
    """Parse abstract JSON lines.
    """
    for path in glob(os.path.join(path, '*.json')):
        with open(path) as fh:
            for line in fh:
                yield Abstract.from_line(line)


@attr.s
class Sentence:

    position = attr.ib()
    tokens = attr.ib()

    def tensor(self, dim=300):
        """Stack word vectors.
        """
        x = [
            vectors[t] if t in vectors else np.zeros(dim)
            for t in self.tokens
        ]

        x = np.array(x)
        x = torch.from_numpy(x)
        x = x.float()

        return x


@attr.s
class Abstract:

    sentences = attr.ib()

    @classmethod
    def from_line(cls, line):
        """Parse JSON, take tokens.
        """
        json = ujson.loads(line.strip())

        return cls([
            Sentence(i, s['token'])
            for i, s in enumerate(json['sentences'])
        ])


@attr.s
class Batch:

    abstracts = attr.ib()

    def packed_sentence_tensor(self, size=50):
        """Pack sentence tensors.
        """
        sents = [
            Variable(s.tensor()).type(ftype)
            for a in self.abstracts
            for s in a.sentences
        ]

        return pad_and_pack(sents, size)

    def unpack_sentences(self, encoded):
        """Unpack encoded sentences.
        """
        start = 0
        for ab in self.abstracts:
            end = start + len(ab.sentences)
            yield encoded[start:end]
            start = end

    def shuffle(self):
        """Shuffle sentences in all abstracts.
        """
        for ab in self.abstracts:
            random.shuffle(ab.sentences)


class Corpus:

    def __init__(self, path, skim=None):
        """Load abstracts into memory.
        """
        reader = read_abstracts(path)

        if skim:
            reader = islice(reader, skim)

        self.abstracts = list(tqdm(reader, total=skim))

    def random_batch(self, size):
        """Query random batch.
        """
        return Batch(random.sample(self.abstracts, size))

    def batches(self, size):
        """Iterate all batches.
        """
        for abstracts in chunked_iter(self.abstracts, size):
            yield Batch(abstracts)


class Encoder(nn.Module):

    def __init__(self, input_dim, lstm_dim):
        super().__init__()
        self.lstm = nn.LSTM(input_dim, lstm_dim, batch_first=True,
            bidirectional=True)

    def forward(self, x, reorder):
        _, (hn, cn) = self.lstm(x)
        # Cat forward + backward hidden layers.
        out = hn.transpose(0, 1).contiguous().view(hn.data.shape[1], -1)
        return out[reorder]


class Regressor(nn.Module):

    def __init__(self, input_dim, lin_dim):
        super().__init__()
        self.out = nn.Linear(input_dim, 1)

    def forward(self, x):
        y = self.out(x)
        return y.squeeze()


def train_batch(batch, sent_encoder, regressor):
    """Train the batch.
    """
    x, reorder = batch.packed_sentence_tensor()

    # Encode sentences.
    sents = sent_encoder(x, reorder)

    # Generate x / y pairs.
    examples = []
    for ab in batch.unpack_sentences(sents):
        for i in range(len(ab)):

            # 0 <--> 1
            y = i / (len(ab)-1)

            # Graf, sentence, size, position.
            examples.append((ab[i], y))

    x, y = zip(*examples)

    x = torch.stack(x)

    y = Variable(torch.FloatTensor(y)).type(ftype)

    return y, regressor(x)


def train(train_path, model_path, train_skim, lr, epochs, epoch_size,
    batch_size, lstm_dim, lin_dim):
    """Train model.
    """
    train = Corpus(train_path, train_skim)

    sent_encoder = Encoder(300, lstm_dim)
    regressor = Regressor(lstm_dim*2, lin_dim)

    params = (
        list(sent_encoder.parameters()) +
        list(regressor.parameters())
    )

    optimizer = torch.optim.RMSprop(params, lr=lr)

    loss_func = nn.MSELoss()

    if torch.cuda.is_available():
        sent_encoder = sent_encoder.cuda()
        regressor = regressor.cuda()

    for epoch in range(epochs):

        print(f'\nEpoch {epoch}')

        epoch_loss = 0
        for _ in tqdm(range(epoch_size)):

            optimizer.zero_grad()

            batch = train.random_batch(batch_size)

            y, y_pred = train_batch(batch, sent_encoder, regressor)

            loss = loss_func(y_pred, y)
            loss.backward()

            optimizer.step()

            epoch_loss += loss.data[0]

        checkpoint(model_path, 'sent_encoder', sent_encoder, epoch)
        checkpoint(model_path, 'regressor', regressor, epoch)

        print(epoch_loss / epoch_size)


# def regress_sents(ab, graf_encoder, regressor):
    # """Regress sentences, get order.
    # """
    # # Generate x / y pairs.
    # examples = []
    # for i in range(len(ab)):

        # # Graf = sentence + context.
        # perm = torch.randperm(len(ab)).type(itype)
        # graf = torch.cat([ab[i].unsqueeze(0), ab[perm]])

        # # Paragraph length.
        # size = Variable(torch.FloatTensor([len(ab)])).type(ftype)

        # # Graf, sentence, size, position.
        # examples.append((graf, ab[i], size))

    # grafs, sents, sizes = zip(*examples)

    # # Encode grafs.
    # grafs, reorder = pad_and_pack(grafs, 30)
    # grafs = graf_encoder(grafs, reorder)

    # # <graf, sent, size>
    # x = zip(grafs, sents, sizes)
    # x = list(map(torch.cat, x))
    # x = torch.stack(x)

    # return regressor(x).data.tolist()


# def predict(test_path, sent_encoder_path, graf_encoder_path, regressor_path,
    # gp_path, test_skim, map_source, map_target):
    # """Predict order.
    # """
    # test = Corpus(test_path, test_skim)

    # sent_encoder = torch.load(
        # sent_encoder_path,
        # map_location={map_source: map_target},
    # )

    # graf_encoder = torch.load(
        # graf_encoder_path,
        # map_location={map_source: map_target},
    # )

    # regressor = torch.load(
        # regressor_path,
        # map_location={map_source: map_target},
    # )

    # gps = []
    # for batch in tqdm(test.batches(100)):

        # batch.shuffle()

        # # Encode sentence batch.
        # sent_batch, reorder = batch.packed_sentence_tensor()
        # sent_batch = sent_encoder(sent_batch, reorder)

        # # Re-group by abstract.
        # unpacked = batch.unpack_sentences(sent_batch)

        # for ab, sents in zip(batch.abstracts, unpacked):

            # gold = [s.position for s in ab.sentences]

            # pred = regress_sents(sents, graf_encoder, regressor)
            # pred = np.argsort(pred).argsort().tolist()

            # gps.append((gold, pred))

    # with open(gp_path, 'w') as fh:
        # ujson.dump(gps, fh)
