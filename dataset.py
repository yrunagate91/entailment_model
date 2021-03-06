import json
import gzip
import time
import os
import io
import re
import subprocess as sp
import string

import tensorflow as tf
import numpy as np
import nltk
from tqdm import tqdm

from util import tprint

DEFAULT_LABEL2IDX = {'neutral': 0, 'entailment': 1, 'contradiction': 2, 'hidden': 3, '-': -1}
DEFAULT_WORD2IDX = {"<PAD>":0, "<UNK>":1}

DEFAULT_CHAR2IDX = {c:i for i, c in enumerate(string.printable)}
DEFAULT_CHAR2IDX['\0'] = 0

DEFAULT_CHARPAD = 16

POS_Tagging = ['<PAD>', '<UNK>', 'WP$', 'RBS', 'SYM', 'WRB', 'IN', 'VB', 'POS', 'TO', ':', '-RRB-', '$', 'MD', 'JJ', '#', 'CD', '``', 'JJR', 'NNP', "''", 'LS', 'VBP', 'VBD', 'FW', 'RBR', 'JJS', 'DT', 'VBG', 'RP', 'NNS', 'RB', 'PDT', 'PRP$', '.', 'XX', 'NNPS', 'UH', 'EX', 'NN', 'WDT', 'VBN', 'VBZ', 'CC', ',', '-LRB-', 'PRP', 'WP']
POS2IDX = {pos:i for i, pos in enumerate(POS_Tagging)}

shared_files = ["DIIN/data/multinli_0.9/shared_train.json",
                "DIIN/data/multinli_0.9/shared_dev_matched.json",
                "DIIN/data/multinli_0.9/shared_dev_mismatched.json",
                ]

def load_shared_content():
    tprint("loading shared_files")
    shared_content = {}
    for sf in shared_files:
        with open(sf, "r") as sfd:
            for l in tqdm(sfd):
                pairID = l.split(" ")[0]
                shared_content[pairID] = json.loads(l[len(pairID)+1:])
    return shared_content

def label2index(x, l2i=DEFAULT_LABEL2IDX):
    return l2i[x]

def word2index(x, w2i=DEFAULT_WORD2IDX):
    return w2i.get(x, w2i['<UNK>'])

def char2index(x, c2i=DEFAULT_CHAR2IDX, pad=DEFAULT_CHARPAD):
    cm = [c2i.get(c, c2i['\0']) for c in x]
    if len(x) > pad:
        return np.array(cm[:pad])
    else:
        return np.array(cm + [0] * (pad - len(x)))

def pos2index(x):
    global POS2IDX
    return POS2IDX.get(x, POS2IDX['<UNK>'])

def extract_json(x, key):
    d = json.loads(x.decode("utf-8"))
    return d[key]

pat = re.compile(r'\(|\)')
def _tokenize(string):
    global pat
    string = re.sub(pat, '', string)
    return filter(None, string.split(" "))


def tokenize(s, func=lambda x: x):
    return [func(t) for t in _tokenize(s)]

def parse_pos(s, func=lambda x: x):
    posp = (x.rstrip(" ").rstrip(")") for x in s.split("(") if ")" in x)
    pos = [func(p.split(" ")[0]) for p in posp]
    return pos

def random_embedding(size, dim, keep_zeros=[]):
    emb = np.random.randn(size, dim)
    for i in keep_zeros:
        emb[i] = np.zeros(dim)

    return emb

def count_word(zfile, size=None):
    tprint("loadding glove")
    word2idx = {"<PAD>":0, "<UNK>":1}
    embedding = [np.zeros(300), np.zeros(300)#np.random.randn(300),#np.zeros(300),
                 ]#np.random.randn(300)]
    with io.BufferedReader(gzip.open(zfile, "rb")) as f:
        for i, l in tqdm(enumerate(f)):
            l = l.decode("utf-8")
            values = l.split(" ")
            w = values.pop(0)
            word2idx[w] = i+2
            embedding.append(np.asarray(values, dtype=np.float32))
            if size and i+2 >= size - 1:
                break

    embedding = np.array(embedding)
    return word2idx, embedding

def count_char(path):
    tprint("counting char")
    chars = set()
    with open(path, "r") as f:
        for i, l in tqdm(enumerate(f)):
            j = json.loads(l)
            chars |= set(j["sentence1"])
            chars |= set(j["sentence2"])

    char2idx = {c:i+1 for i, c in enumerate(chars)}
    char2idx['\0'] = 0
    return char2idx

def resize(x, size):
    x.set_shape(size)
    return x

def padding(x, y, left=False):
    xsa = tf.shape(x)
    ysa = tf.shape(y)
    xr = len(x.get_shape())
    yr = len(y.get_shape())

    xs = xsa[1]
    ys = ysa[1]
    sm = tf.maximum(xs,ys)
    xp = tf.abs(xs - sm)
    yp = tf.abs(ys - sm)
    xpad = np.zeros((xr,2), dtype=np.int64).tolist()
    ypad = np.zeros((yr,2), dtype=np.int64).tolist()
    if left:
        xpad[1][0] = xp
        ypad[1][0] = yp
    else:
        xpad[1][1] = xp
        ypad[1][1] = yp

    return tf.pad(x, xpad), tf.pad(y, ypad)

def crop(x, max_len):
    xr = len(x.get_shape())
    xl = tf.shape(x)[1]
    if xr == 2:
        return tf.cond(xl <= max_len, lambda: x, lambda: x[:,:max_len])
    elif xr == 3:
        return tf.cond(xl <= max_len, lambda: x, lambda: x[:,:max_len, :])
    else:
        raise Exception("cropping rank not support")


def MapDatasetJSON(dataset,
                   key,
                   dtype=None,
                   osize=(),
                   func=lambda x: x):

    if not dtype:
        raise ValueError("output type not specified.")

    if isinstance(dtype, list) or isinstance(dtype, tuple):
        assert isinstance(osize, list) or isinstance(osize, tuple)

    _func = (lambda x: [func(extract_json(x, key))]) \
            if isinstance(dtype, list) or isinstance(dtype, tuple) \
               else (lambda x: func(extract_json(x, key)))

    return dataset.map(
        lambda line: tf.py_func(
            _func,
            [line],
            dtype)
    ).map(lambda x: resize(x, osize))

def MapDatasetString(dataset,
                     key,
                     osize=[None],
                     dtype=[tf.string],
                     tkn=tokenize,
                     func=lambda x: x):

    return MapDatasetJSON(dataset,
                          key,
                          dtype=dtype,
                          osize=osize,
                          func=lambda x: tkn(x, func=func))


def MNLIJSONDataset(filename,
                    batch=10,
                    epoch=1,
                    shuffle_buffer_size=1,
                    word2index=word2index,
                    char2index=char2index,
                    char_pad=DEFAULT_CHARPAD,
                    max_len=None,
                    sc=None,
                    pad2=True):

    dataset = tf.data.TextLineDataset(filename)
    sentence1 = MapDatasetString(dataset,
                                "sentence1_binary_parse",
                                dtype=[tf.int64],
                                func=word2index).repeat(epoch).padded_batch(batch, [None])

    sentence2 = MapDatasetString(dataset,
                                "sentence2_binary_parse",
                                dtype=[tf.int64],
                                func=word2index).repeat(epoch).padded_batch(batch, [None])

    gold_label = MapDatasetJSON(dataset,
                                "gold_label",
                                dtype=tf.int64,
                                func=label2index).repeat(epoch).batch(batch)

    ###20180629 char_embedding
    sent1_char = MapDatasetString(dataset,
                                  "sentence1_binary_parse",
                                  osize=[None, char_pad],
                                  dtype=[tf.int64],
                                  func=char2index).repeat(epoch).padded_batch(batch, [None, char_pad])
    sent2_char = MapDatasetString(dataset,
                                  "sentence2_binary_parse",
                                  osize=[None, char_pad],
                                  dtype=[tf.int64],
                                  func=char2index).repeat(epoch).padded_batch(batch, [None, char_pad])
    
    ###change dataset1->sentence1 dataset2->sentence2 dataset3->gold_label

    ###20180612: add antonym exactmatch synonym into the dataset

    antonym1 = MapDatasetJSON(dataset,
                              "pairID",
                              dtype=[tf.float32],
                              func=lambda x: np.array(sc[x]["sentence1_token_antonym_with_s2"]).astype(np.float32)).repeat(epoch).padded_batch(batch, [None])

    antonym2 = MapDatasetJSON(dataset,
                              "pairID",
                              dtype=[tf.float32],
                              func=lambda x: np.array(sc[x]["sentence2_token_antonym_with_s1"]).astype(np.float32)).repeat(epoch).padded_batch(batch, [None])

    exact1to2 = MapDatasetJSON(dataset,
                               "pairID",
                               dtype=[tf.float32],
                               func=lambda x: np.array(sc[x]["sentence1_token_exact_match_with_s2"]).astype(np.float32)).repeat(epoch).padded_batch(batch, [None])
    
    exact2to1 = MapDatasetJSON(dataset,
                               "pairID",
                               dtype=[tf.float32],
                               func=lambda x: np.array(sc[x]["sentence2_token_exact_match_with_s1"]).astype(np.float32)).repeat(epoch).padded_batch(batch, [None])
    
    synonym1 = MapDatasetJSON(dataset,
                              "pairID",
                              dtype=[tf.float32],
                              func=lambda x: np.array(sc[x]["sentence1_token_synonym_with_s2"]).astype(np.float32)).repeat(epoch).padded_batch(batch, [None])
    
    synonym2 = MapDatasetJSON(dataset,
                              "pairID",
                              dtype=[tf.float32],
                              func=lambda x: np.array(sc[x]["sentence2_token_synonym_with_s1"]).astype(np.float32)).repeat(epoch).padded_batch(batch, [None])

    pos1 = MapDatasetString(dataset,
                            "sentence1_parse",
                            dtype=[tf.int64],
                            tkn=parse_pos,
                            func=pos2index).repeat(epoch).padded_batch(batch, [None])

    pos2 = MapDatasetString(dataset,
                            "sentence2_parse",
                            dtype=[tf.int64],
                            tkn=parse_pos,
                            func=pos2index).repeat(epoch).padded_batch(batch, [None])

    D = tf.data.Dataset.zip((sentence1,
                             sentence2,
                             gold_label,
                             antonym1,
                             antonym2,
                             exact1to2,
                             exact2to1,
                             synonym1,
                             synonym2,
                             sent1_char,
                             sent2_char,
                             pos1,
                             pos2,
    ))

    if pad2:
        D = D.map(lambda s1, s2, l, a1, a2, e1, e2, sy1, sy2, s1c, s2c, p1, p2: (*padding(s1,s2), l,
                                                                                 *padding(a1,a2),
                                                                                 *padding(e1,e2),
                                                                                 *padding(sy1,sy2),
                                                                                 *padding(s1c, s2c),
                                                                                 *padding(p1, p2),
        ))

    if max_len:
        D = D.map(lambda s1, s2, l, a1, a2, e1, e2, sy1, sy2, s1c, s2c, p1, p2: (crop(s1, max_len),
                                                                                 crop(s2, max_len),
                                                                                 l,
                                                                                 crop(a1, max_len),
                                                                                 crop(a2, max_len),
                                                                                 crop(e1, max_len),
                                                                                 crop(e2, max_len),
                                                                                 crop(sy1, max_len),
                                                                                 crop(sy2, max_len),
                                                                                 crop(s1c, max_len),
                                                                                 crop(s2c, max_len),
                                                                                 crop(p1, max_len),
                                                                                 crop(p2, max_len),
        ))

    return D.shuffle(shuffle_buffer_size)

def MnliTrainSet(filename="multinli_0.9_train.jsonl",
                 batch=10,
                 epoch=1,
                 shuffle_buffer_size=1,
                 prefetch_buffer_size=1,
                 w2i=word2index,
                 c2i=char2index,
                 char_pad=DEFAULT_CHARPAD,
                 max_len=None,
                 sc=None,
                 pad2=True):

    train = MNLIJSONDataset(filename,
                            batch,
                            epoch,
                            shuffle_buffer_size,
                            word2index=w2i,
                            pad2=pad2,
                            char_pad=char_pad,
                            max_len=max_len,
                            sc=sc
    )

    return train.prefetch(prefetch_buffer_size)

def MnliDevSet(files=("multinli_0.9_dev_mismatched_clean.jsonl", "multinli_0.9_dev_matched_clean.jsonl"),
               batch=10,
               epoch=1,
               shuffle_buffer_size=1,
               prefetch_buffer_size=100,
               w2i=word2index,
               c2i=char2index,
               char_pad=DEFAULT_CHARPAD,
               max_len = None,
               sc=None,
               pad2=True):

    dev_mismatch = MNLIJSONDataset(files[0],
                                   batch,
                                   epoch,
                                   shuffle_buffer_size,
                                   w2i,
                                   pad2=pad2,
                                   char_pad=char_pad,
                                   max_len=max_len,
                                   sc=sc
    )
    dev_match = MNLIJSONDataset(files[1],
                                batch,
                                epoch,
                                shuffle_buffer_size,
                                w2i,
                                pad2=pad2,
                                char_pad=char_pad,
                                max_len=max_len,
                                sc=sc
    )
    return {"match": dev_match.prefetch(prefetch_buffer_size),
            "mismatch": dev_mismatch.prefetch(prefetch_buffer_size)}
    #return dev_match.concatenate(dev_mismatch).prefetch(prefetch_buffer_size)


def Mnli(tfile="multinli_0.9_train.jsonl",
         dfiles=("multinli_0.9_dev_mismatched_clean.jsonl", "multinli_0.9_dev_matched_clean.jsonl"),
         tbatch=10,
         dbatch=5,
         tepoch=5,
         depoch=1,
         shuffle_buffer_size=20,
         prefetch_buffer_size=3,
         w2i=word2index,
         c2i=char2index,
         char_pad=DEFAULT_CHARPAD,
         max_len=None,
         sc=None,
         pad2=True):

    trainset = MnliTrainSet(tfile,
                            batch=tbatch,
                            epoch=tepoch,
                            shuffle_buffer_size=shuffle_buffer_size,
                            prefetch_buffer_size=prefetch_buffer_size,
                            w2i=w2i,
                            c2i=c2i,
                            char_pad=char_pad,
                            max_len=max_len,
                            sc=sc,
                            pad2=pad2)

    devset = MnliDevSet(dfiles,
                        batch=dbatch,
                        epoch=depoch,
                        shuffle_buffer_size=shuffle_buffer_size,
                        prefetch_buffer_size=prefetch_buffer_size,
                        w2i=w2i,
                        c2i=c2i,
                        char_pad=char_pad,
                        max_len=max_len,
                        sc=sc,
                        pad2=pad2)

    iterator =  tf.data.Iterator.from_structure(trainset.output_types,
                                               trainset.output_shapes)

    train_init = iterator.make_initializer(trainset)
    dev_match_init = iterator.make_initializer(devset["match"])
    dev_mismatch_init = iterator.make_initializer(devset["mismatch"])

    Next = iterator.get_next()
    
    return Next, {"train": train_init,
                  "dev_match": dev_match_init,
                  "dev_mismatch": dev_mismatch_init}


class MultiNli:
    def __init__(self,
                 glove_path,
                 mnli_path,
                 batch=5,
                 train_epoch=10,
                 dev_epoch=1,
                 shuffle_buffer_size=10,
                 prefetch_buffer_size=1,
                 glove_size=None,
                 pad2=True,
                 trainfile=None,
                 all_printable_char=False,
                 char_emb_dim=100,
                 char_pad=DEFAULT_CHARPAD,
                 max_len=None,
    ):

        self.glove_path = glove_path
        self.glove_size = glove_size
        self.mnli_path = mnli_path
        self.batch = batch
        self.train_epoch = train_epoch
        self.dev_epoch = dev_epoch
        self.shuffle_buffer_size = shuffle_buffer_size
        self.prefetch_buffer_size = prefetch_buffer_size
        self.pad2 = pad2
        self.max_len = max_len
        
        self.char_pad = char_pad
        self.char_emb_dim = char_emb_dim
        
        self.trainfile = os.path.join(mnli_path, "multinli_0.9_train.jsonl") if not trainfile else os.path.join(mnli_path, trainfile)
        
        self.devfile = tuple(os.path.join(mnli_path, dfile) for dfile in ("multinli_0.9_dev_mismatched_clean.jsonl", "multinli_0.9_dev_matched_clean.jsonl"))

        self.train_size = int(sp.check_output(["wc", "-l", self.trainfile]).split()[0])
        self.dev_size = [int(sp.check_output(["wc", "-l", devf]).split()[0]) for devf in self.devfile]

        #load shared_content
        self.shared_content = load_shared_content()

        #load word embedding
        self.word2idx, self.embedding = count_word(self.glove_path, self.glove_size)


        #load char embedding
        if all_printable_char:
            self.char2idx = DEFAULT_CHAR2IDX
        else:
            self.char2idx = count_char(self.trainfile)

        #gen random char emb
        self.char_embedding = random_embedding(len(self.char2idx), self.char_emb_dim, keep_zeros=(0,))

        #gen random pos emb
        self.pos2idx = POS2IDX
        self.pos_embedding = random_embedding(len(self.pos2idx), len(self.pos2idx), keep_zeros=(0,))

        #setup dataset
        self.data, self.init = Mnli(tfile=self.trainfile,
                                    dfiles=self.devfile,
                                    tbatch=self.batch,
                                    dbatch=self.batch,
                                    tepoch=self.train_epoch,
                                    depoch=self.dev_epoch,
                                    shuffle_buffer_size=self.shuffle_buffer_size,
                                    prefetch_buffer_size=self.prefetch_buffer_size,
                                    w2i=lambda x: word2index(x, self.word2idx),
                                    pad2=self.pad2,
                                    c2i=lambda x: char2index(x, self.char2idx, pad=self.char_pad),
                                    max_len=self.max_len,
                                    sc=self.shared_content
        )

        self.sentence1 = self.data[0]
        self.sentence2 = self.data[1]
        self.label = self.data[2]
        #20180612 add
        self.antonym1 = self.data[3]
        self.antonym2 = self.data[4]
        self.exact1to2 = self.data[5]
        self.exact2to1 = self.data[6]
        self.synonym1 = self.data[7]
        self.synonym2 = self.data[8]
        self.sent1char = self.data[9]
        self.sent2char = self.data[10]
        self.pos1 = self.data[11]
        self.pos2 = self.data[12]

    def train(self, sess):
        sess.run(self.init['train'])

    def dev_matched(self, sess):
        sess.run(self.init['dev_match'])

    def dev_mismatched(self, sess):
        sess.run(self.init['dev_mismatch'])

    def get_batch(self):
        return self.data

# dataset = tf.data.TextLineDataset("/home/vanessa/DIIN/data/multinli_0.9/multinli_0.9_train.jsonl")
# dataset1 = MapDatasetString(dataset, "sentence1_binary_parse", dtype=[tf.int64], func=char2index)
# dataset2 = MapDatasetString(dataset, "sentence2", dtype=[tf.int64], func=word2index)
# dataset3 = MapDatasetJSON(dataset, "gold_label", dtype=tf.int64, func=label2index)

# dataset = tf.data.Dataset.zip((dataset1, dataset2, dataset3))

# def data():
#     i = dataset.make_one_shot_iterator()
#     n = i.get_next()
#     return n

# #dataset = dataset.padded_batch(3,[None, None, 1], 0)
# i1 = dataset1.padded_batch(4, [None, 16]).make_one_shot_iterator()
# i2 = dataset2.padded_batch(4, [None]).make_one_shot_iterator()
# i3 = dataset3.batch(4).make_one_shot_iterator()
# n1 = i1.get_next()
# n2 = i2.get_next()
# n3 = i3.get_next()
#dataset = MNLIJSONDataset("multinli_0.9_train.jsonl")
# dataset = MnliDevSet()#MNLIJSONDataset("multinli_0.9_dev_mismatched.jsonl")
# i = dataset.make_one_shot_iterator()
# n = i.get_next()
#mnli = MultiNli("glove.txt.gz", "./", batch=3)
# sess = tf.InteractiveSession()
