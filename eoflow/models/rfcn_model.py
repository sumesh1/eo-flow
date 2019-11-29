import logging
import tensorflow as tf
import numpy as np
from marshmallow import Schema, fields

from ..base import BaseModel, ModelHeads
from .layers import conv1d, conv2d, conv3d, deconv2d, crop_and_concat, max_pool_3d, conv2d_gru, weighted_cross_entropy

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')


class RFCNModel(BaseModel):
    """ Implementation of a Recurrent Fully-Convolutional-Network """

    class RFCNModelSchema(Schema):
        learning_rate = fields.Float(missing=None, description='Learning rate used in training.', example=0.01)
        n_layers = fields.Int(required=True, description='Number of layers of the FCN model', example=10)
        keep_prob = fields.Float(required=True, description='Keep probability used in dropout layers.', example=0.5)
        features_root = fields.Int(required=True, description='Number of features at the root level.', example=32)

        conv_size = fields.Int(missing=3, description='Size of the convolution kernels.')
        deconv_size = fields.Int(missing=2, description='Size of the deconvolution kernels.')
        conv_stride = fields.Int(missing=1, description='Stride used in convolutions.')
        add_dropout = fields.Bool(missing=False, description='Add dropout to layers.')
        add_batch_norm = fields.Bool(missing=True, description='Add batch normalization to layers.')
        bias_init = fields.Float(missing=0.0, description='Bias initialization value.')
        padding = fields.String(missing='VALID', description='Padding type used in convolutions.')

        pool_size = fields.Int(missing=2, description='Kernel size used in max pooling.')
        pool_stride = fields.Int(missing=2, description='Stride used in max pooling.')

        class_weights = fields.List(fields.Float, missing=None, description='Class weights used in training.')

    def _net(self, x, is_training):

        net = x
        keep_prob = self.config.keep_prob

        # encoding path
        connection_outputs = []
        for layer in range(self.config.n_layers):
            # compute number of features as a function of network depth level
            features = 2 ** layer * self.config.features_root

            # one 3d convolutional filter but without convolving along time -> effectively 2d convolution
            conv = conv3d(net,
                          features,
                          is_training=is_training,
                          k_size=self.config.conv_size,
                          im_stride=self.config.conv_stride,
                          scope='encoding_' + str(layer),
                          add_dropout=self.config.add_dropout,
                          keep_prob=keep_prob,
                          add_bn=self.config.add_batch_norm,
                          single_filter=True,
                          convolve_time=False,
                          padding=self.config.padding)
            connection_outputs.append(conv)
            # max pooling operation
            net = max_pool_3d(conv,
                              ksize=self.config.pool_size,
                              stride=self.config.pool_stride,
                              pool_time=False)
        # another 2d convolution along spatial dimension only
        bottom = conv3d(net,
                        2 ** self.config.n_layers * self.config.features_root,
                        is_training=is_training,
                        k_size=self.config.conv_size,
                        im_stride=self.config.conv_stride,
                        scope='bottom_',
                        add_dropout=self.config.add_dropout,
                        keep_prob=keep_prob,
                        add_bn=self.config.add_batch_norm,
                        single_filter=True,
                        convolve_time=False,
                        padding=self.config.padding)
        # Reduce temporal dimension
        # bottom = conv2d_lstm(bottom, 2 ** layers * self.features_root, k_size=conv_size, scope='lstm_bottom')
        bottom = conv2d_gru(bottom,
                            2 ** self.config.n_layers * self.config.features_root,
                            k_size=self.config.conv_size,
                            scope='gru_bottom',
                            padding=self.config.padding)

        net = bottom
        # decoding path
        for layer in range(self.config.n_layers):
            # find corresponding level in decoding branch
            conterpart_layer = self.config.n_layers - 1 - layer
            # get same number of features as counterpart layer
            features = 2 ** conterpart_layer * self.config.features_root

            # transposed convolution to upsample tensors
            shape = net.get_shape().as_list()
            deconv_output_shape = [tf.shape(net)[0],
                                   shape[1] * self.config.deconv_size,
                                   shape[2] * self.config.deconv_size,
                                   features]
            deconv = deconv2d(net,
                              deconv_output_shape,
                              k_size=self.config.deconv_size,
                              is_training=is_training,
                              scope='deconv_' + str(conterpart_layer),
                              add_bn=self.config.add_batch_norm)
            # skip connection with recurrent filter
            reduced = conv2d_gru(connection_outputs[conterpart_layer],
                                 features,
                                 k_size=self.config.conv_size,
                                 scope='decoding_gru_' + str(conterpart_layer),
                                 padding=self.config.padding)
            # crop and concatenate
            cc = crop_and_concat(reduced, deconv)
            # bank of 2 convolutional layers as in standard FCN
            net = conv2d(cc,
                         features,
                         k_size=self.config.conv_size,
                         im_stride=self.config.conv_stride,
                         is_training=is_training,
                         scope='decoding_' + str(conterpart_layer),
                         add_dropout=self.config.add_dropout,
                         keep_prob=keep_prob,
                         add_bn=self.config.add_batch_norm,
                         padding=self.config.padding)
        # final 1x1 convolution corresponding to pixel-wise linear combination of feature channels
        # TODO: when converting use keras Conv2D with kernel size 1
        logits = conv1d(net,
                        self.config.n_classes,
                        scope='logits',
                        bias_init=self.config.bias_init)

        return logits

    def build_model(self, features, labels, is_train_tensor, model_heads):
        x = features

        # Build net
        logits = self._net(x, is_train_tensor)

        # softmax to convert activations to pseudo-probabilities
        probs = tf.nn.softmax(logits)
        # class prediction as argmax of softmax
        preds = tf.argmax(probs[..., 1:], 3)

        if ModelHeads.TRAIN in model_heads:
            out_shape = tf.shape(logits)
            labels_cropped = tf.image.resize_with_crop_or_pad(labels, out_shape[1], out_shape[2])

            if self.config.image_summaries:
                self.add_training_summary(tf.summary.image('input', features[:,0,...][...,0:3]))
                self.add_training_summary(tf.summary.image('labels_raw', labels[...,0:3]))
                self.add_training_summary(tf.summary.image('labels', labels_cropped[...,0:3]))
                self.add_training_summary(tf.summary.image('output', logits[...,0:3]))

            # flatten tensors to apply class weighting
            flat_logits = tf.reshape(logits, [-1, self.config.n_classes])
            flat_labels = tf.reshape(labels_cropped, [-1, self.config.n_classes])

            if self.config.class_weights is not None:
                loss = weighted_cross_entropy(flat_logits, flat_labels, self.config.class_weights)
            else:
                loss = tf.reduce_mean(tf.nn.softmax_cross_entropy_with_logits(logits=flat_logits, labels=flat_labels))

            self.add_training_summary(tf.summary.scalar('loss', loss))

            # update operations for batch-normalisation and define train stepo as minimisation of loss
            update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
            with tf.control_dependencies(update_ops):
                optimizer = tf.train.AdamOptimizer(self.config.learning_rate)
                train_op = optimizer.minimize(loss,
                                              global_step=self.global_step_tensor)

            train_head = ModelHeads.TrainHead(train_op, loss, self.get_merged_training_summaries())

        if ModelHeads.PREDICT in model_heads:

            predictions = {
                'probabilities': probs,
                'predictions': preds
            }

            predict_head = ModelHeads.PredictHead(predictions)

        if ModelHeads.EVALUATE in model_heads:
            out_shape = tf.shape(logits)
            labels_cropped = tf.image.resize_with_crop_or_pad(labels, out_shape[1], out_shape[2])

            labels_n = tf.argmax(labels_cropped[..., 1:], 3)
            accuracy_fn = lambda: tf.metrics.accuracy(labels_n, preds)

            self.add_validation_metric(accuracy_fn, 'accuracy')

            evaluate_ops = self.get_merged_validation_ops()
            evaluate_head = ModelHeads.EvaluateHead(*evaluate_ops)

        heads = []
        for model_head in model_heads:
            if model_head == ModelHeads.TRAIN:
                heads.append(train_head)
            elif model_head == ModelHeads.PREDICT:
                heads.append(predict_head)
            elif model_head == ModelHeads.EVALUATE:
                heads.append(evaluate_head)
            else:
                raise NotImplementedError

        return heads
