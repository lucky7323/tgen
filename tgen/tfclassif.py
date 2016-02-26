#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Classifying trees to determine which DAIs are represented.

TODO this is a "fork" of classif.py. Merge identical code.
"""

from __future__ import unicode_literals
import cPickle as pickle
import time
import datetime
import sys
import re
import math
import tempfile
import shutil

import numpy as np
import tensorflow as tf
from tensorflow.python.ops import rnn, rnn_cell

from pytreex.core.util import file_stream

from tgen.rnd import rnd
from tgen.logf import log_debug, log_info
from tgen.futil import read_das, read_ttrees, trees_from_doc
from tgen.features import Features
from tgen.ml import DictVectorizer
from tgen.embeddings import TreeEmbeddingExtract, EmbeddingExtract
from tgen.tree import TreeData
from alex.components.slu.da import DialogueAct


class TreeEmbeddingClassifExtract(EmbeddingExtract):
    """Extract t-lemma + formeme embeddings in a row, disregarding syntax"""

    VOID = 0
    UNK_T_LEMMA = 1
    UNK_FORMEME = 2

    def __init__(self, cfg):
        super(TreeEmbeddingClassifExtract, self).__init__()

        self.dict_t_lemma = {'UNK_T_LEMMA': self.UNK_T_LEMMA}
        self.dict_formeme = {'UNK_FORMEME': self.UNK_FORMEME}
        self.max_tree_len = cfg.get('max_tree_len', 25)

    def init_dict(self, train_trees, dict_ord=None):
        """Initialize dictionary, given training trees (store t-lemmas and formemes,
        assign them IDs).

        @param train_das: training DAs
        @param dict_ord: lowest ID to be assigned (if None, it is initialized to MIN_VALID)
        @return: the highest ID assigned + 1 (the current lowest available ID)
        """
        if dict_ord is None:
            dict_ord = self.MIN_VALID

        for tree in train_trees:
            for t_lemma, formeme in tree.nodes:
                if t_lemma not in self.dict_t_lemma:
                    self.dict_t_lemma[t_lemma] = dict_ord
                    dict_ord += 1
                if formeme not in self.dict_formeme:
                    self.dict_formeme[formeme] = dict_ord
                    dict_ord += 1

        return dict_ord

    def get_embeddings(self, tree):
        """Get the embeddings of a sentence (list of word form/tag pairs)."""
        embs = []
        for t_lemma, formeme in tree.nodes[:self.max_tree_len]:
            embs.append(self.dict_formeme.get(formeme, self.UNK_FORMEME))
            embs.append(self.dict_t_lemma.get(t_lemma, self.UNK_T_LEMMA))

        if len(embs) < self.max_tree_len * 2:  # left-pad with void
            embs = [self.VOID] * (self.max_tree_len * 2 - len(embs)) + embs

        return embs

    def get_embeddings_shape(self):
        """Return the shape of the embedding matrix (for one object, disregarding batches)."""
        return [self.max_tree_len * 2]


class TFTreeClassifier(object):
    """A classifier for trees that decides which DAIs are currently represented
    (to be used in limiting candidate generator and/or re-scoring the trees)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.language = cfg.get('language', 'en')
        self.selector = cfg.get('selector', '')
        # TODO this should allow normal sentences; it should correspond to Seq2Seq model
        self.tree_embs = cfg.get('nn', '').startswith('emb')
        if self.tree_embs:
            self.tree_embs = TreeEmbeddingClassifExtract(cfg)
            self.emb_size = cfg.get('emb_size', 50)

        self.nn_shape = cfg.get('nn_shape', 'ff')
        self.num_hidden_units = cfg.get('num_hidden_units', 512)

        self.passes = cfg.get('passes', 200)
        self.min_passes = cfg.get('min_passes', 0)
        self.alpha = cfg.get('alpha', 0.1)
        self.randomize = cfg.get('randomize', True)
        self.batch_size = cfg.get('batch_size', 1)

        self.validation_freq = cfg.get('validation_freq', 10)
        self.max_cores = cfg.get('max_cores')
        self.cur_da = None
        self.cur_da_bin = None
        self.checkpoint_path = None

    def save_to_file(self, model_fname):
        """Save the generator to a file (actually two files, one for configuration and one
        for the TensorFlow graph, which must be stored separately).

        @param model_fname: file name (for the configuration file); TF graph will be stored with a \
            different extension
        """
        log_info("Saving classifier to %s..." % model_fname)
        with file_stream(model_fname, 'wb', encoding=None) as fh:
            data = {'cfg': self.cfg,
                    'da_feats': self.da_feats,
                    'da_vect': self.da_vect,
                    'tree_embs': self.tree_embs,
                    'input_shape': self.input_shape,
                    'num_outputs': self.num_outputs, }
            if self.tree_embs:
                data['dict_size'] = self.dict_size
            else:
                data['tree_feats'] = self.tree_feats
                data['tree_vect'] = self.tree_vect
            pickle.dump(data, fh, protocol=pickle.HIGHEST_PROTOCOL)
        tf_session_fname = re.sub(r'(.pickle)?(.gz)?$', '.tfsess', model_fname)
        if self.checkpoint_path:
            shutil.copyfile(self.checkpoint_path, tf_session_fname)
        else:
            self.saver.save(self.session, tf_session_fname)

    def _save_checkpoint(self):
        """Save a checkpoint to a temporary path; set `self.checkpoint_path` to the path
        where it is saved; if called repeatedly, will always overwrite the last checkpoint."""
        if not self.checkpoint_path:
            fh, path = tempfile.mkstemp(".ckpt", "tftreecl-", self.checkpoint_path)
            self.checkpoint_path = path
        log_info('Saving checkpoint to %s' % self.checkpoint_path)
        self.saver.save(self.session, self.checkpoint_path)

    def restore_checkpoint(self):
        if not self.checkpoint_path:
            return
        self.saver.restore(self.session, self.checkpoint_path)

    @staticmethod
    def load_from_file(model_fname):
        """Load the generator from a file (actually two files, one for configuration and one
        for the TensorFlow graph, which must be stored separately).

        @param model_fname: file name (for the configuration file); TF graph must be stored with a \
            different extension
        """
        log_info("Loading generator from %s..." % model_fname)
        with file_stream(model_fname, 'rb', encoding=None) as fh:
            data = pickle.load(fh)
            ret = TFTreeClassifier(cfg=data['cfg'])
            ret.__dict__.update(data)

        # re-build TF graph and restore the TF session
        tf_session_fname = re.sub(r'(.pickle)?(.gz)?$', '.tfsess', model_fname)
        ret._init_neural_network()
        ret.saver.restore(ret.session, tf_session_fname)
        return ret

    def train(self, das_file, ttree_file, data_portion=1.0, valid_das=None, valid_trees=None):
        """Run training on the given training data."""

        log_info('Training tree classifier...')

        self._init_training(das_file, ttree_file, data_portion)
        top_cost = float('nan')

        if valid_trees:  # preparing valid_trees for evaluation (1 or 2 paraphrases)
            if isinstance(valid_trees, tuple):
                valid_trees = [[t1, t2] for t1, t2 in zip(valid_trees[0], valid_trees[1])]
            else:
                valid_trees = [[t] for t in valid_trees]

        for iter_no in xrange(1, self.passes + 1):
            self.train_order = range(len(self.train_trees))
            if self.randomize:
                rnd.shuffle(self.train_order)
            pass_cost, pass_diff = self._training_pass(iter_no)

            if iter_no > self.min_passes and iter_no % self.validation_freq == 0 and valid_das > 0:

                valid_diff = np.sum([self.dist_to_da(d, t) for d, t in zip(valid_das, valid_trees)])

                # cost combining validation and training data performance
                cur_cost = 1000 * valid_diff + 100 * pass_diff + pass_cost
                log_info('Combined validation cost: %8.3f' % cur_cost)

                # if we have the best model so far, save it as a checkpoint (overwrite previous)
                if math.isnan(top_cost) or cur_cost < top_cost:
                    top_cost = cur_cost
                    self._save_checkpoint()

    def classify(self, trees):
        """Classify the tree -- get DA slot-value pairs and DA type to which the tree
        corresponds (as 1/0 array).

        This does not have a lot of practical use here, see is_subset_of_da.
        """
        if self.tree_embs:
            inputs = np.array([self.tree_embs.get_embeddings(tree) for tree in trees])
        else:
            inputs = self.tree_vect.transform([self.tree_feats.get_features(tree, {})
                                               for tree in trees])
        fd = {}
        self._add_inputs_to_feed_dict(inputs, fd)
        results = self.session.run(self.outputs, feed_dict=fd)
        # normalize & binarize the result
        return np.array([[1. if r > 0 else 0. for r in result] for result in results])

    def is_subset_of_da(self, da, trees):
        """Given a DA and an array of trees, this gives a boolean array indicating which
        trees currently cover/describe a subset of the DA.

        @param da: the input DA against which the trees should be tested
        @param trees: the trees to test against the DA
        @return: boolean array, with True where the tree covers/describes a subset of the DA
        """
        # get 1-hot representation of the DA
        da_bin = self.da_vect.transform([self.da_feats.get_features(None, {'da': da})])[0]
        # convert it to array of booleans
        da_bin = da_bin != 0
        # classify the trees
        covered = self.classify(trees)
        # decide whether 1's in their 1-hot vectors are subsets of True's in da_bin
        return [((c != 0) | da_bin == da_bin).all() for c in covered]

    def init_run(self, da):
        """Remember the current DA for subsequent runs of `is_subset_of_cur_da`."""
        self.cur_da = da
        da_bin = self.da_vect.transform([self.da_feats.get_features(None, {'da': da})])[0]
        self.cur_da_bin = da_bin != 0

    def is_subset_of_cur_da(self, trees):
        """Same as `is_subset_of_da`, but using `self.cur_da` set via `init_run`."""
        da_bin = self.cur_da_bin
        covered = self.classify(trees)
        return [((c != 0) | da_bin == da_bin).all() for c in covered]

    def corresponds_to_cur_da(self, trees):
        """Given an array of trees, this gives a boolean array indicating which
        trees currently cover exactly the current DA (set via `init_run`).

        @param trees: the trees to test against the current DA
        @return: boolean array, with True where the tree exactly covers/describes the current DA
        """
        da_bin = self.cur_da_bin
        covered = self.classify(trees)
        return [((c != 0) == da_bin).all() for c in covered]

    def dist_to_da(self, da, trees):
        da_bin = self.da_vect.transform([self.da_feats.get_features(None, {'da': da})])[0]
        da_bin = da_bin != 0
        covered = self.classify(trees)
        return [sum(abs(c - da_bin)) for c in covered]

    def dist_to_cur_da(self, trees):
        da_bin = self.cur_da_bin
        covered = self.classify(trees)
        return [sum(abs(c - da_bin)) for c in covered]

    def _init_training(self, das_file, ttree_file, data_portion):
        """Initialize training.

        Store input data, initialize 1-hot feature representations for input and output and
        transform training data accordingly, initialize the classification neural network.
        """
        # read input from files or take it directly from parameters
        if isinstance(das_file, list):
            das = das_file
        else:
            log_info('Reading DAs from ' + das_file + '...')
            das = read_das(das_file)
        if isinstance(ttree_file, list):
            trees = ttree_file
        else:
            log_info('Reading t-trees from ' + ttree_file + '...')
            ttree_doc = read_ttrees(ttree_file)
            trees = trees_from_doc(ttree_doc, self.language, self.selector)

        # make training data smaller if necessary
        train_size = int(round(data_portion * len(trees)))
        self.train_trees = trees[:train_size]
        self.train_das = das[:train_size]

        # add empty tree + empty DA to training data
        # (i.e. forbid the network to keep any of its outputs "always-on")
        train_size += 1
        self.train_trees.append(TreeData())
        empty_da = DialogueAct()
        empty_da.parse('inform()')
        self.train_das.append(empty_da)

        self.train_order = range(len(self.train_trees))
        log_info('Using %d training instances.' % train_size)

        # initialize input features/embeddings
        if self.tree_embs:
            self.dict_size = self.tree_embs.init_dict(self.train_trees)
            self.X = np.array([self.tree_embs.get_embeddings(tree) for tree in self.train_trees])
        else:
            self.tree_feats = Features(['node: presence t_lemma formeme'])
            self.tree_vect = DictVectorizer(sparse=False, binarize_numeric=True)
            self.X = [self.tree_feats.get_features(tree, {}) for tree in self.train_trees]
            self.X = self.tree_vect.fit_transform(self.X)

        # initialize output features
        self.da_feats = Features(['dat: dat_presence', 'svp: svp_presence'])
        self.da_vect = DictVectorizer(sparse=False, binarize_numeric=True)
        self.y = [self.da_feats.get_features(None, {'da': da}) for da in self.train_das]
        self.y = self.da_vect.fit_transform(self.y)

        # initialize I/O shapes
        if not self.tree_embs:
            self.input_shape = list(self.X[0].shape)
        else:
            self.input_shape = self.tree_embs.get_embeddings_shape()
        self.num_outputs = len(self.da_vect.get_feature_names())

        # initialize NN classifier
        self._init_neural_network()
        # initialize the NN variables
        self.session.run(tf.initialize_all_variables())

    def _init_neural_network(self):
        """Create the neural network for classification, according to the self.nn_shape
        parameter (as set in configuration)."""

        # set TensorFlow random seed
        tf.set_random_seed(rnd.randint(-sys.maxint, sys.maxint))

        self.targets = tf.placeholder(tf.float32, [None, self.num_outputs], name='targets')

        # TODO enable embeddings
        #if self.tree_embs:
        #    layers.append([Embedding('emb', self.dict_size, self.emb_size, 'uniform_005')])

        # feedforward networks
        if self.nn_shape.startswith('ff'):
            self.inputs = tf.placeholder(tf.float32, [None] + self.input_shape, name='inputs')
            # TODO enable embeddings
            #if self.tree_embs:
            #    layers.append([Flatten('flat')])
            num_ff_layers = 2
            if self.nn_shape[-1] in ['0', '1', '3', '4']:
                num_ff_layers = int(self.nn_shape[-1])
            self.outputs = self._ff_layers('ff', num_ff_layers, self.inputs)

        elif self.nn_shape.startswith('rnn'):
            self.initial_state = tf.placeholder(tf.float32, [None, self.emb_size])
            self.inputs = [tf.placeholder(tf.int32, [None], name=('enc_inp-%d' % i))
                           for i in xrange(self.input_shape[0])]
            self.cell = rnn_cell.BasicLSTMCell(self.emb_size)
            self.outputs = self._rnn('rnn', self.inputs)

        # TODO convolutional networks
        # elif 'conv' in self.nn_shape or 'pool' in self.nn_shape:
            #assert self.tree_embs  # convolution makes no sense without embeddings
            #num_conv = 0
            #if 'conv' in self.nn_shape:
                #num_conv = 1
            #if 'conv2' in self.nn_shape:
                #num_conv = 2
            #pooling = None
            #if 'maxpool' in self.nn_shape:
                #pooling = T.max
            #elif 'avgpool' in self.nn_shape:
                #pooling = T.mean
            #layers += self._conv_layers('conv', num_conv, pooling)
            #layers.append([Flatten('flat')])
            #layers += self._ff_layers('ff', 1)

        # input types: integer 3D for tree embeddings (batch + 2D embeddings),
        #              float 2D (matrix) for binary input (batch + features)

        # the cost as computed by TF actually adds a "fake" sigmoid layer on top
        # (or is computed as if there were a sigmoid layer on top)
        self.cost = tf.reduce_mean(tf.reduce_sum(
                 tf.nn.sigmoid_cross_entropy_with_logits(self.outputs, self.targets, name='CE'), 1))

        # this would have been the "true" cost function, if there were a "real" sigmoid layer on top
        # however, it is not numerically stable
        #self.cost = tf.reduce_mean(tf.reduce_sum(self.targets * -tf.log(self.outputs) + (1 - self.targets) * -tf.log(1 - self.outputs), 1))

        self.optimizer = tf.train.AdamOptimizer(self.alpha)
        self.train_func = self.optimizer.minimize(self.cost)

        # initialize session
        session_config = None
        if self.max_cores:
            session_config = tf.ConfigProto(inter_op_parallelism_threads=self.max_cores,
                                            intra_op_parallelism_threads=self.max_cores)
        self.session = tf.Session(config=session_config)

        # this helps us load/save the model
        self.saver = tf.train.Saver(tf.all_variables())

    def _ff_layers(self, name, num_layers, X):
        width = [np.prod(self.input_shape)] + (num_layers * [self.num_hidden_units]) + [self.num_outputs]
        # the last layer should be a sigmoid, but TF simulates it for us in cost computation
        # so the output is "unnormalized sigmoids"
        activ = (num_layers * [tf.nn.tanh]) + [tf.identity]
        Y = X
        for i in xrange(num_layers + 1):
            w = tf.Variable(tf.random_normal([width[i], width[i+1]], stddev=0.1),
                            name + ('-w%d' % i))
            b = tf.Variable(tf.zeros([width[i+1]]), name + ('-b%d' % i))
            Y = activ[i](tf.matmul(Y, w) + b)
        return Y

    def _rnn(self, name, enc_inputs):
        encoder_cell = rnn_cell.EmbeddingWrapper(self.cell, self.dict_size)
        _, encoder_states = rnn.rnn(encoder_cell, enc_inputs, dtype=tf.float32)
        w = tf.Variable(tf.random_normal([self.cell.state_size, self.num_outputs], stddev=0.1),
                            name + ('-w'))
        b = tf.Variable(tf.zeros([self.num_outputs]), name + ('-b'))
        return tf.matmul(encoder_states[-1], w) + b


    #def _conv_layers(self, name, num_layers=1, pooling=None):
        #ret = []
        #for i in xrange(num_layers):
            #ret.append([Conv1D(name + str(i + 1),
                               #filter_length=self.cnn_filter_length,
                               #num_filters=self.cnn_num_filters,
                               #init=self.init, activation=T.tanh)])
        #if pooling is not None:
            #ret.append([Pool1D(name + str(i + 1) + 'pool', pooling_func=pooling)])
        #return ret

    def batches(self):
        for i in xrange(0, len(self.train_order), self.batch_size):
            yield self.train_order[i: i + self.batch_size]

    def _add_inputs_to_feed_dict(self, inputs, fd):

        if self.nn_shape.startswith('rnn'):
            fd[self.initial_state] = np.zeros([inputs.shape[0], self.emb_size])
            sliced_inputs = np.squeeze(np.array(np.split(np.array([ex for ex in inputs
                                                                   if ex is not None]),
                                                         len(inputs[0]), axis=1)), axis=2)
            for input_, slice_ in zip(self.inputs, sliced_inputs):
                fd[input_] = slice_
        else:
            fd[self.inputs] = inputs


    def _training_pass(self, pass_no):
        """Perform one training pass through the whole training data, print statistics."""

        pass_start_time = time.time()

        log_debug('\n***\nTR %05d:' % pass_no)
        log_debug("Train order: " + str(self.train_order))

        pass_cost = 0
        pass_diff = 0

        for tree_nos in self.batches():

            log_debug('TREE-NOS: ' + str(tree_nos))
            log_debug("\n".join(unicode(self.train_trees[i]) + "\n" + unicode(self.train_das[i])
                                for i in tree_nos))
            log_debug('Y: ' + str(self.y[tree_nos]))

            fd = {self.targets: self.y[tree_nos]}
            self._add_inputs_to_feed_dict(self.X[tree_nos], fd)
            results, cost, _ = self.session.run([self.outputs, self.cost, self.train_func],
                                                feed_dict=fd)
            bin_result = np.array([[1. if r > 0 else 0. for r in result] for result in results])

            log_debug('R: ' + str(bin_result))
            log_debug('COST: %f' % cost)
            log_debug('DIFF: %d' % np.sum(np.abs(self.y[tree_nos] - bin_result)))

            pass_cost += cost
            pass_diff += np.sum(np.abs(self.y[tree_nos] - bin_result))

        # print and return statistics
        self._print_pass_stats(pass_no, datetime.timedelta(seconds=(time.time() - pass_start_time)),
                               pass_cost, pass_diff)

        return pass_cost, pass_diff

    def _print_pass_stats(self, pass_no, time, cost, diff):
        log_info('PASS %03d: duration %s, cost %f, diff %d' % (pass_no, str(time), cost, diff))

