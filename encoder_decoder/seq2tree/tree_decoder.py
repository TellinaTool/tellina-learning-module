"""A set of tree decoder modules used in the encoder-decoder framework."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import os, sys
if sys.version_info > (3, 0):
    from six.moves import xrange

import tensorflow as tf

from encoder_decoder import decoder, data_utils, graph_utils

DEBUG = False


class BasicTreeDecoder(decoder.Decoder):

    def __init__(self, hyperparams, dim, output_project=None):
        super(BasicTreeDecoder, self).__init__(hyperparams, dim, output_project)

        self.H_NO_EXPAND = tf.constant(data_utils.H_NO_EXPAND_ID, shape=[self.batch_size])
        self.V_NO_EXPAND = tf.constant(data_utils.V_NO_EXPAND_ID, shape=[self.batch_size])

    
    def define_graph(self, encoder_state, decoder_inputs, embeddings,
                     attention_states=None, num_heads=1,
                     initial_state_attention=False, feed_previous=False,
                     reuse_variables=False):
        """
        :param encoder_state: hidden state of the encoder
        :param inputs: placeholders for the discrete inputs of the decoder
        :param embeddings: target embeddings
        :param attention_states: 3D Tensor [batch_size x attn_length x attn_dim].
        :param num_heads: Number of attention heads that read from from attention_states.
            Dummy field if attention_states is None.
        :param initial_state_attention: If False (default), initial attentions are zero.
            If True, initialize the attentions from the initial state and attention states
            -- useful to resume decoding from a previously stored decoder state and attention
            state.
        :param feed_previous: Boolean; if True, only the first of decoder_inputs will be
            used (the "ROOT" symbol), and all other decoder inputs will be generated by:
            next = embedding_lookup(embedding, argmax(previous_output)),
            In effect, this implements a greedy decoder. It can also be used
            during training to emulate http://arxiv.org/abs/1506.03099.
            If False, decoder_inputs are used as given (the standard decoder case).
        :param reuse_variables: reuse variables in scope.
        :return: Output states and the final hidden state of the decoder. Need
            output_project to obtain distribution over output vocabulary.
        """
        self.E = tf.constant(np.identity(len(decoder_inputs)), dtype=tf.int32)

        if self.use_attention and \
                not attention_states.get_shape()[1:2].is_fully_defined():
            raise ValueError("Shape[1] and [2] of attention_states must be known %s"
                             % attention_states.get_shape())

        with tf.compat.v1.variable_scope("basic_tree_decoder") as scope:
            vertical_cell, vertical_scope = self.vertical_cell()
            horizontal_cell, horizontal_scope = self.horizontal_cell()
            outputs = []
            attn_alignments = []

            # search control
            self.back_pointers = tf.constant(0, shape=[self.batch_size, 1, 1],
                                             dtype=tf.int32)

            # continuous stack used for storing LSTM states, synced with
            # self.back_pointers
            if self.rnn_cell == "gru":
                init_state = encoder_state
            else:
                init_state = tf.concat(axis=1, values=[encoder_state[0], encoder_state[1]])

            if self.use_attention:
                hidden, hidden_features, v = \
                    self.attention_hidden_layer(attention_states, num_heads)
                batch_size = tf.shape(input=attention_states)[0]
                attn_dim = tf.shape(input=attention_states)[2]
                batch_attn_size = tf.stack([batch_size, attn_dim])
                # initial attention state
                attns = tf.concat(axis=1, values=[tf.zeros(batch_attn_size, dtype=tf.float32)
                         for _ in xrange(num_heads)])
                if initial_state_attention:
                    attns, attn_alignment = \
                        self.attention(v, encoder_state, hidden_features,
                                       num_heads, hidden)
                    attn_alignments.append(attn_alignment)
                init_state = tf.concat(axis=1, values=[init_state] + [attns])
            self.state = tf.expand_dims(init_state, 1)
            self.input = tf.expand_dims(decoder_inputs[0], 1)
            self.input = tf.expand_dims(self.input, 1)
            self.input.set_shape([self.batch_size, 1, 1])
            search_left_to_right_next = self.is_no_expand(self.input[:, -1, 0])
            
            for i in xrange(len(decoder_inputs)):
                if DEBUG:
                    print("decoder step: %d" % i)
                if i > 0: tf.compat.v1.get_variable_scope().reuse_variables()

                self.step = i + 1
                search_left_to_right = search_left_to_right_next
                
                if self.use_attention:
                    input, state, attns = self.peek()
                else:
                    input, state = self.peek()

                input_embeddings = tf.squeeze(tf.nn.embedding_lookup(params=embeddings, ids=input),
                                              axis=[1])

                # compute batch horizontal and vertical steps.
                if self.use_attention:
                    v_output, v_state, v_attns, v_attn_alignment = self.attention_cell(
                        vertical_cell, vertical_scope, input_embeddings, state, attns,
                        hidden_features, v, num_heads, hidden)
                    h_output, h_state, h_attns, h_attn_alignment = self.attention_cell(
                        horizontal_cell, horizontal_scope, input_embeddings, state, attns,
                        hidden_features, v, num_heads, hidden)
                else:
                    v_output, v_state = vertical_cell(input_embeddings, state, vertical_scope)
                    h_output, h_state = horizontal_cell(input_embeddings, state, horizontal_scope)

                # select horizontal or vertical computation results for each example
                # based on its own control state.
                switch_masks = []
                for j in xrange(self.batch_size):
                    mask = tf.cond(pred=search_left_to_right[j], true_fn=lambda: tf.constant([[1, 0]]),
                                                            false_fn=lambda: tf.constant([[0, 1]]))
                    switch_masks.append(mask)
                switch_mask = tf.concat(axis=0, values=switch_masks)

                batch_output = switch_mask(switch_mask, [h_output, v_output])
                if self.rnn_cell == "gru":
                    batch_state = switch_mask(switch_mask, [h_state, v_state])
                elif self.rnn_cell == "lstm":
                    batch_cell = switch_mask(switch_mask, [h_state[0], v_state[0]])
                    batch_hs = switch_mask(switch_mask, [h_state[1], v_state[1]])
                    batch_state = tf.concat(axis=1, values=[batch_cell, batch_hs])

                if self.use_attention:
                    batch_attns = switch_mask(switch_mask, [h_attns, v_attns])
                    batch_state = tf.concat(axis=1, values=[batch_state, batch_attns])

                    batch_attn_alignment = h_attn_alignment
                    attn_alignments.append(batch_attn_alignment)
                
                # record output state to compute the loss.
                outputs.append(batch_output)

                if i < len(decoder_inputs) - 1:
                    # storing states
                    if feed_previous:
                        # Project decoder output for next state input.
                        W, b = self.output_project
                        batch_projected_output = tf.matmul(batch_output, W) + b
                        batch_output_symbol = tf.argmax(input=batch_projected_output, axis=1)
                        batch_output_symbol = tf.cast(batch_output_symbol, dtype=tf.int32)
                    else:
                        batch_output_symbol = decoder_inputs[i+1]
                    search_left_to_right_next = self.is_no_expand(batch_output_symbol)

                    back_pointer = map_fn(
                        self.back_pointer, [search_left_to_right_next,
                                            search_left_to_right,
                                            self.grandparent(),
                                            self.parent(),
                                            tf.constant(i, shape=[self.batch_size], dtype=tf.int32)],
                        self.batch_size)
                    back_pointer.set_shape([self.batch_size])
                    if DEBUG:
                        print("back_pointer.get_shape(): {}".format(back_pointer.get_shape()))

                    next_input = map_fn(
                        self.next_input, [search_left_to_right_next,
                                          search_left_to_right,
                                          self.parent_input(),
                                          self.get_input(),
                                          batch_output_symbol],
                        self.batch_size)
                    next_input.set_shape([self.batch_size])
                    if DEBUG:
                        print("next_input.get_shape(): {}".format(next_input.get_shape()))

                    next_state = map_fn(
                        self.next_state, [search_left_to_right_next,
                                          search_left_to_right,
                                          self.parent_state(),
                                          self.get_state(),
                                          batch_state],
                        self.batch_size)
                    if DEBUG:
                        print("next_state.get_shape(): {}".format(next_state.get_shape()))

                    self.push([next_input, back_pointer, next_state])

        if self.rnn_cell == "gru":
            final_batch_state = batch_state
        elif self.rnn_cell == "lstm":
            final_batch_state = tf.compat.v1.nn.rnn_cell.LSTMStateTuple(batch_cell, batch_hs)

        if self.use_attention:
            temp = [tf.expand_dims(batch_attn_alignment, 1) for batch_attn_alignment in attn_alignments]
            return outputs, final_batch_state, tf.concat(axis=1, values=temp)
        else:
            return outputs, final_batch_state


    def back_pointer(self, x):
        h_search_next, h_search, grandparent, parent, current = x
        return tf.cond(pred=h_search_next,
                       true_fn=lambda : tf.cond(pred=h_search[0], true_fn=lambda : grandparent, false_fn=lambda : parent),
                       false_fn=lambda : tf.cond(pred=h_search[0], true_fn=lambda : parent, false_fn=lambda : current))

    def next_input(self, x):
        h_search_next, h_search, parent, current, next = x
        return tf.cond(pred=h_search_next,
                       true_fn=lambda : tf.cond(pred=h_search[0], true_fn=lambda : parent, false_fn=lambda : current),
                       false_fn=lambda : next)

    def next_state(self, x):
        h_search_next, h_search, parent, current, next = x
        return tf.cond(pred=h_search_next,
                       true_fn=lambda : tf.cond(pred=h_search[0], true_fn=lambda : parent, false_fn=lambda : current),
                       false_fn=lambda : next)

    def parent_input(self):
        inds = tf.nn.embedding_lookup(params=self.E, ids=tf.split(axis=0, num_or_size_splits=self.batch_size, value=self.parent()))
        inds = tf.squeeze(inds, axis=[1])
        inds = inds[:, :self.step]
        return tf.reduce_sum(input_tensor=tf.multiply(self.input[:, :, 0], inds), axis=1)


    def parent_state(self):
        inds = tf.nn.embedding_lookup(params=self.E, ids=tf.split(axis=0, num_or_size_splits=self.batch_size, value=self.parent()))
        inds = tf.squeeze(inds, axis=[1])
        inds = inds[:, :self.step]
        inds = tf.expand_dims(inds, 2)
        inds = tf.tile(inds, tf.stack([tf.constant(1), tf.constant(1), tf.shape(input=self.state)[2]]))
        return tf.reduce_sum(input_tensor=tf.multiply(self.state, tf.cast(inds, tf.float32)), axis=1)

    def grandparent(self):
        inds = tf.nn.embedding_lookup(params=self.E, ids=tf.split(axis=0, num_or_size_splits=self.batch_size, value=self.parent()))
        inds = tf.squeeze(inds, axis=[1])
        inds = inds[:, :self.step]
        return tf.reduce_sum(input_tensor=tf.multiply(self.back_pointers[:, :, 0], inds), axis=1)

    def parent(self):
        p = self.back_pointers[:, -1, 0]
        return p

    def get_input(self):
        return self.input[:, -1, 0]

    def get_state(self):
        return self.state[:, -1, :]

    def push(self, batch_states):
        """
        :param batch_states: list of list of state tensors
        """
        batch_next_input = batch_states[0]
        batch_next_input = tf.expand_dims(batch_next_input, 1)
        batch_next_input = tf.expand_dims(batch_next_input, 1)
        self.input = tf.concat(axis=1, values=[self.input, batch_next_input])

        batch_back_pointers = batch_states[1]
        batch_back_pointers = tf.expand_dims(batch_back_pointers, 1)
        batch_back_pointers = tf.expand_dims(batch_back_pointers, 1)
        self.back_pointers = tf.concat(axis=1, values=[self.back_pointers, batch_back_pointers])

        batch_states = tf.expand_dims(batch_states[2], 1)
        self.state = tf.concat(axis=1, values=[self.state, batch_states])


    def peek(self):
        """
        :param batch_indices: list of stack pointers for each search thread
        :return: batch stack state tuples
                 (batch_parent_states, [batch_attention_states])
        """
        batch_input_symbols = self.input[:, -1, :]
        batch_stack_states = self.state[:, -1, :]

        if self.rnn_cell == "gru":
            batch_states = batch_stack_states[:, :self.dim]
            attn_start_pos = self.dim
            batch_states.set_shape([self.batch_size, self.dim])
        elif self.rnn_cell == "lstm":
            batch_stack_cells = batch_stack_states[:, :self.dim]
            batch_stack_hiddens = batch_stack_states[:, self.dim:2*self.dim]
            attn_start_pos = 2 * self.dim
            batch_states = tf.compat.v1.nn.rnn_cell.LSTMStateTuple(batch_stack_cells, batch_stack_hiddens)
        else:
            raise ValueError("Unrecognized RNN cell type.")

        if self.use_attention:
            batch_attention_states = batch_stack_states[:, attn_start_pos:]
            return batch_input_symbols, batch_states, batch_attention_states
        else:
            return batch_input_symbols, batch_states


    def is_no_expand(self, ind):
        return tf.logical_or(self.no_vertical_expand(ind), self.no_horizontal_expand(ind))


    def no_vertical_expand(self, ind):
        return tf.equal(tf.cast(ind, tf.int32), self.V_NO_EXPAND)


    def no_horizontal_expand(self, ind):
        return tf.equal(tf.cast(ind, tf.int32), self.H_NO_EXPAND)


    def vertical_cell(self):
        """Cell that controls transition from parent to child."""
        with tf.compat.v1.variable_scope("vertical_cell") as scope:
            cell = graph_utils.create_multilayer_cell(self.rnn_cell, scope,
                                                      self.dim, self.num_layers,
                                                      self.tg_input_keep,
                                                      self.tg_output_keep)
        return cell, scope


    def horizontal_cell(self):
        """Cell that controls transition from left sibling to right sibling."""
        with tf.compat.v1.variable_scope("horizontal_cell") as scope:
            cell = graph_utils.create_multilayer_cell(self.rnn_cell, scope,
                                                      self.dim, self.num_layers,
                                                      self.tg_input_keep,
                                                      self.tg_output_keep)
        return cell, scope


def switch_mask(mask, candidates):
    """
    :param mask: A 2D binary matrix of size [batch_size, num_options].
                 Each row of mask has exactly one non-zero entry.
    :param candidates: A list of 2D matrices with length num_options.
    :return: selections concatenated as a new batch.
    """
    assert(len(candidates) > 1)
    threed_mask = tf.tile(tf.expand_dims(mask, 2),
                          [1, 1, candidates[0].get_shape()[1].value])
    threed_mask = tf.cast(threed_mask, candidates[0].dtype)
    expanded_candidates = [tf.expand_dims(c, 1) for c in candidates]
    candidate = tf.concat(axis=1, values=expanded_candidates)
    return tf.reduce_sum(input_tensor=tf.multiply(threed_mask, candidate), axis=1)


def map_fn(fn, elems, batch_size):
    """Pesudo multi-ariti scan."""
    results = []
    elem_lists = [tf.split(axis=0, num_or_size_splits=batch_size, value=elem) for elem in elems]
    for i in xrange(batch_size):
        args = [tf.squeeze(elem_lists[0][i], axis=[0])] + \
               [elem_list[i] for elem_list in elem_lists[1:]]
        results.append(fn(args))
    _results = tf.concat(axis=0, values=results)
    return _results


if __name__ == "__main__":
    decoder = BasicTreeDecoder(dim=100, batch_size=1, rnn_cell="gru", num_layers=1,
                               input_keep_prob=1, output_keep_prob=1,
                               use_attention=False, use_copy=False, output_project=None)
    decoder_inputs = [tf.compat.v1.placeholder(dtype=tf.int32, shape=[None],
                                     name="decoder{0}".format(i)) for i in xrange(14)]
    encoder_state = tf.random.normal(shape=[1, 100])
    attention_states = tf.random.normal(shape=[8, 100])
    target_embeddings = tf.random.normal(shape=[200, 100])
    outputs, state, left_to_right = decoder.define_graph(encoder_state,
                                                         decoder_inputs,
                                                         target_embeddings,
                                                         attention_states,
                                                         feed_previous=False)

    with tf.compat.v1.Session() as sess:
        sess.run(tf.compat.v1.global_variables_initializer())
        input_feed = {}
        inputs = [[9], [10], [21], [7], [53], [105], [7], [6], [32], [51], [7], [6], [6], [6]]
        for l in xrange(14):
            input_feed[decoder_inputs[l].name] = inputs[l]
        input_feed[encoder_state.name] = np.random.rand(1, 100)
        output_feed = [state, left_to_right]
        state = sess.run(output_feed, input_feed)
        print(state[0])
        print(state[1])
