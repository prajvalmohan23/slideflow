# Copyright (C) James Dolezal - All Rights Reserved
#
# Unauthorized copying of this file, via any medium is strictly prohibited
# Proprietary and confidential
# Written by James Dolezal <jamesmdolezal@gmail.com>, October 2017
# ==========================================================================

# Update 3/2/2019: Beginning tf.data implementation
# Update 5/29/2019: Supports both loose image tiles and TFRecords, 
#   annotations supplied by separate annotation file upon initial model call

''''Builds a CNN model.'''

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time
import os
import sys
from datetime import datetime

import numpy as np
import pickle
import argparse

import tensorflow as tf
from tensorflow.contrib.framework import arg_scope
from tensorflow.summary import FileWriterCache
from tensorboard import summary as summary_lib
from tensorboard.plugins.custom_scalar import layout_pb2
import tensorflow.contrib.lookup

import inception_v4
from inception_utils import inception_arg_scope
from glob import glob

from util import tfrecords, sfutil

slim = tf.contrib.slim

# Calculate accuracy with https://stackoverflow.com/questions/50111438/tensorflow-validate-accuracy-with-batch-data
# TODO: try next, comment out line 254 (results in calculating total_loss before update_ops is called)
# TODO: visualize graph, memory usage, and compute time with https://www.tensorflow.org/guide/graph_viz

class SlideflowModel:
	''' Model containing all functions necessary to build input dataset pipelines,
	build a training and validation set model, and monitor and execute training.'''

	# Global constants describing the model to be built.

	# Process images of the below size. If this number is altered, the
	# model architecture will change and will need to be retrained.

	IMAGE_SIZE = 512
	NUM_CLASSES = 5

	NUM_EXAMPLES_PER_EPOCH = 1024

	# Constants for the training process.
	MOVING_AVERAGE_DECAY = 0.9999 		# Decay to use for the moving average.
	NUM_EPOCHS_PER_DECAY = 240.0		# Epochs after which learning rate decays.
	LEARNING_RATE_DECAY_FACTOR = 0.05	# Learning rate decay factor.
	INITIAL_LEARNING_RATE = 0.01		# Initial learning rate.
	ADAM_LEARNING_RATE = 0.001			# Learning rate for the Adams Optimizer.

	# Variables previous created with parser & FLAGS
	BATCH_SIZE = 16
	WHOLE_IMAGE = '' # Filename of whole image (JPG) to evaluate with saved model
	MAX_EPOCH = 300
	LOG_FREQUENCY = 20 # How often to log results to console, in steps
	SUMMARY_STEPS = 20 # How often to save summaries for Tensorboard display, in steps
	TEST_FREQUENCY = 1200 # How often to run validation testing, in steps
	USE_FP16 = True

	def __init__(self, data_directory, input_directory, annotations_file):
		self.DATA_DIR = data_directory
		self.INPUT_DIR = input_directory
		self.MODEL_DIR = os.path.join(self.DATA_DIR, 'models/active') # Directory where to write event logs and checkpoints.
		self.TRAIN_DIR = os.path.join(self.MODEL_DIR, 'train') # Directory where to write eval logs and summaries.
		self.TEST_DIR = os.path.join(self.MODEL_DIR, 'test') # Directory where to write eval logs and summaries.
		self.TRAIN_FILES = os.path.join(self.INPUT_DIR, "train_data/*/*.jpg")
		self.TEST_FILES = os.path.join(self.INPUT_DIR, "eval_data/*/*.jpg")
		self.DTYPE = tf.float16 if self.USE_FP16 else tf.float32
		self.TRAIN_TFRECORD = os.path.join(self.INPUT_DIR, "train.tfrecords")
		self.EVAL_TFRECORD = os.path.join(self.INPUT_DIR, "eval.tfrecords")
		self.USE_TFRECORD = (os.path.exists(self.TRAIN_TFRECORD) and os.path.exists(self.EVAL_TFRECORD))

		annotations = tfrecords.load_annotations(annotations_file)
		if not self.verify_annotation_integrity(annotations):
			sys.exit()
		self.ANNOTATIONS_TABLE = tf.contrib.lookup.HashTable(
			tf.contrib.lookup.KeyValueTensorInitializer(list(annotations.keys()), list(annotations.values())), -1
		)

		if tf.gfile.Exists(self.MODEL_DIR):
			tf.gfile.DeleteRecursively(self.MODEL_DIR)
		tf.gfile.MakeDirs(self.MODEL_DIR)

	def _gen_filenames_op(self, dir_string):
		filenames_op = tf.train.match_filenames_once(dir_string)
		labels_op = tf.map_fn(lambda f: self.ANNOTATIONS_TABLE.lookup(tf.string_split([f], '/').values[tf.constant(-2, dtype=tf.int32)]),
								filenames_op, dtype=tf.int32)
		return filenames_op, labels_op

	def _parse_function(self, filename, label):
		image_string = tf.read_file(filename)
		image = tf.image.decode_jpeg(image_string, channels = 3)
		image = tf.image.per_image_standardization(image)

		dtype = tf.float16 if self.USE_FP16 else tf.float32
		image = tf.image.convert_image_dtype(image, dtype)
		image.set_shape([self.IMAGE_SIZE, self.IMAGE_SIZE, 3])

		return image, label

	def _parse_tfrecord_function(self, tfrecord_features):
		'''Loads image file data from TFRecord and annotation from previously loaded file.

		Args:
			tfrecord_features: 	a dict of features corresponding to a single image tile

		Returns:
			image: a Tensor of shape [size, size, 3] containing image data
			label: accompanying label
		'''
		#label = tfrecord_features['category']
		case = tfrecord_features['case']
		label = self.ANNOTATIONS_TABLE.lookup(case)
		image = tf.image.decode_jpeg(tfrecord_features['image_raw'], channels=3)
		image = tf.image.per_image_standardization(image)
		dtype = tf.float16 if self.USE_FP16 else tf.float32
		image = tf.image.convert_image_dtype(image, dtype)
		image.set_shape([self.IMAGE_SIZE, self.IMAGE_SIZE, 3])

		return image, label

	def _gen_batched_dataset(self, filenames, labels):
		# Replace the below dataset with one that uses a Python generator for flexibility of labeling
		dataset = tf.data.Dataset.from_tensor_slices((filenames, labels))
		dataset = dataset.shuffle(tf.size(filenames, out_type=tf.int64))
		dataset = dataset.map(self._parse_function, num_parallel_calls = 8)
		dataset = dataset.batch(self.BATCH_SIZE)
		return dataset

	def _gen_batched_dataset_from_tfrecord(self, tfrecord):
		raw_image_dataset = tf.data.TFRecordDataset(tfrecord)
		feature_description = tfrecords.FEATURE_DESCRIPTION

		def _parse_image_function(example_proto):
			"""Parses the input tf.Example proto using the above feature dictionary."""
			return tf.parse_single_example(example_proto, feature_description)

		dataset = raw_image_dataset.map(_parse_image_function)
		dataset = dataset.shuffle(100000)
		dataset = dataset.map(self._parse_tfrecord_function, num_parallel_calls = 8)
		dataset = dataset.batch(self.BATCH_SIZE)
		return dataset

	def verify_annotation_integrity(self, annotations):
		'''Iterate through folders if using raw images and verify all have an annotation;
		if using TFRecord, iterate through all records and verify all entries for valid annotation.'''
		success = True
		if self.USE_TFRECORD:
			case_list = []
			for tfrecord_file in [self.TRAIN_TFRECORD, self.EVAL_TFRECORD]:
				tfrecord_iterator = tf.python_io.tf_record_iterator(path=tfrecord_file)
				for string_record in tfrecord_iterator:
					example = tf.train.Example()
					example.ParseFromString(string_record)
					case = example.features.feature['case'].bytes_list.value[0].decode('utf-8')
					if case not in annotations:
						case_list.extend([case])
						success = False
			case_list = set(case_list)
			for case in case_list:
				print(f" + [{sfutil.fail('ERROR')}] Failed TFRecord integrity check: annotation not found for case {sfutil.green(case)}")
		else:
			case_list = [i.split('/')[-1] for i in glob(os.path.join(self.INPUT_DIR, "train_data/*"))]
			case_list.extend([i.split('/')[-1] for i in glob(os.path.join(self.INPUT_DIR, "eval_data/*"))])
			case_list = set(case_list)
			for case in case_list:
				if case not in annotations:
					print(f" + [{sfutil.fail('ERROR')}] Failed image tile integrity check: annotation not found for case {sfutil.green(case)}")
					success = False
		return success

	def build_inputs(self):
		'''Construct input for the model.

		Args:
			sess: active tensorflow session

		Returns:
			next_batch_images: Images. 4D tensor of [batch_size, IMAGE_SIZE, IMAGE_SIZE, 3] size.
			next_batch_labels: Labels. 1D tensor of [batch_size] size.
		'''

		if not self.USE_TFRECORD:
			with tf.name_scope('filename_input'):
				train_filenames_op, train_labels_op = self._gen_filenames_op(self.TRAIN_FILES)
				test_filenames_op, test_labels_op = self._gen_filenames_op(self.TEST_FILES)
			train_dataset = self._gen_batched_dataset(train_filenames_op, train_labels_op)
		else:
			with tf.name_scope('input'):
				train_dataset = self._gen_batched_dataset_from_tfrecord(self.TRAIN_TFRECORD)
		with tf.name_scope('input'):
			train_dataset = train_dataset.repeat(self.MAX_EPOCH)
			train_dataset = train_dataset.prefetch(1)

			test_dataset = self._gen_batched_dataset_from_tfrecord(self.EVAL_TFRECORD)
			test_dataset = test_dataset.prefetch(1)

			with tf.name_scope('iterator'):
				train_iterator = train_dataset.make_initializable_iterator()

				# Will likely need to be re-initializable iterator to repeat testing
				test_iterator = test_dataset.make_initializable_iterator()

				handle = tf.placeholder(tf.string, shape=[])
				iterator = tf.data.Iterator.from_string_handle(handle, 
															   train_iterator.output_types,
															   train_iterator.output_shapes)

			next_batch_images, next_batch_labels = iterator.get_next()

			if self.USE_FP16: next_batch_images = tf.cast(next_batch_images, dtype=tf.float16)	

		return next_batch_images, next_batch_labels, train_iterator, test_iterator, handle

	def loss(self, logits, labels):
		# Calculate average cross entropy loss across the batch.
		labels = tf.cast(labels, tf.int64)
		cross_entropy = tf.nn.sparse_softmax_cross_entropy_with_logits(
			labels=labels, logits=logits, name='cross_entropy_per_example')
		cross_entropy_mean = tf.reduce_mean(cross_entropy, name='cross_entropy')
		tf.add_to_collection('losses', cross_entropy_mean)

		# Total loss is defined as the cross entropy loss plus all of the weight decay terms (L2 loss)
		return tf.add_n(tf.get_collection('losses'), name='total_loss')

	def generate_loss_chart(self):
		return summary_lib.custom_scalar_pb(
			layout_pb2.Layout(category=[
				layout_pb2.Category(
					title='losses',
					chart=[
						layout_pb2.Chart(
							title='losses',
							multiline=layout_pb2.MultilineChartContent(tag=[
								'loss/training', 'loss/valid'
							]))
					])
			]))

	def build_train_op(self, total_loss, global_step):
		opt = tf.train.AdamOptimizer(learning_rate=self.ADAM_LEARNING_RATE,
										beta1=0.9,
										beta2=0.999,
										epsilon=1.0)
		train_op = slim.learning.create_train_op(total_loss, opt)
		return train_op

	def train(self, retrain_model = None, retrain_weights = None, restore_checkpoint = None):
		'''Train the model for a number of steps, according to flags set by the argument parser.'''

		if restore_checkpoint:
			ckpt = tf.train.get_checkpoint_state(restore_checkpoint)

		variables_to_ignore = []#("InceptionV4/Logits/Logits/weights:0", "InceptionV4/Logits/Logits/biases:0")
		variables_to_restore = []

		global_step = tf.train.get_or_create_global_step()
		with tf.device('/cpu'):
			next_batch_images, next_batch_labels, train_it, test_it, it_handle = self.build_inputs()

		training_pl = tf.placeholder(tf.bool, name='train_pl')
		with arg_scope(inception_arg_scope()):
			logits, end_points = inception_v4.inception_v4(next_batch_images, 
														   num_classes=self.NUM_CLASSES,
														   is_training=training_pl)

			if restore_checkpoint:
				for trainable_var in tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES):
					if (trainable_var.name not in variables_to_ignore) and (trainable_var.name[12:21] != "AuxLogits"):
						variables_to_restore.append(trainable_var)

		loss = self.loss(logits, next_batch_labels)

		# Create an averaging op to follow validation accuracy
		with tf.name_scope('mean_validation_loss'):
			validation_loss, validation_loss_update = tf.metrics.mean(loss)
			stream_vars = [v for v in tf.local_variables() if v.name.startswith('mean_validation_loss')]
			stream_vars_reset = [v.initializer for v in stream_vars]

		train_op = self.build_train_op(loss, global_step)
		
		# -- SUMMARIES -----------------------------------------------------------------------------

		with tf.name_scope('loss'):
			train_summ = summary_lib.scalar('training', loss)
			inception_summaries = tf.summary.merge_all()
			valid_summ = summary_lib.scalar('valid', validation_loss)

		layout_summary = self.generate_loss_chart()

		init = (tf.global_variables_initializer(), tf.local_variables_initializer())

		class _LoggerHook(tf.train.SessionRunHook):
			'''Logs loss and runtime.'''
			def __init__(self, train_str, test_str, parent):
				self.parent = parent
				self.train_str = train_str
				self.test_str = test_str
				self.train_handle = None
				self.test_handle = None

			def after_create_session(self, session, coord):
				print ('Initializing data input stream...')
				if self.train_str is not None:
					self.train_iterator_handle, self.test_iterator_handle = session.run([self.train_str, self.test_str])
					session.run([init, train_it.initializer, test_it.initializer])
				print ('complete.')
					
			def begin(self):
				self._step = -1
				self._start_time = time.time()

			def before_run(self, run_context):
				feed_dict = run_context.original_args.feed_dict
				if feed_dict and it_handle in feed_dict and feed_dict[it_handle] == self.train_iterator_handle:
					self._step += 1
					return tf.train.SessionRunArgs(loss)

			def after_run(self, run_context, run_values):
				if ((self._step % self.parent.LOG_FREQUENCY == 0) and
				   (run_context.original_args.feed_dict) and
				   (it_handle in run_context.original_args.feed_dict) and
				   (run_context.original_args.feed_dict[it_handle] == self.train_iterator_handle)):
					current_time = time.time()
					duration = current_time - self._start_time
					self._start_time = current_time

					loss_value = run_values.results
					images_per_sec = self.parent.LOG_FREQUENCY * self.parent.BATCH_SIZE / duration
					sec_per_batch = float(duration / self.parent.LOG_FREQUENCY)

					format_str = ('%s: step %d, loss = %.2f (%.1f images/sec; %.3f sec/batch)')
					print(format_str % (datetime.now(), self._step, loss_value,
										images_per_sec, sec_per_batch))

		loggerhook = _LoggerHook(train_it.string_handle(), test_it.string_handle(), self)
		step = 1

		if restore_checkpoint:
			pretrained_saver = tf.train.Saver(variables_to_restore)

		with tf.train.MonitoredTrainingSession(
			checkpoint_dir = self.MODEL_DIR,
			hooks = [loggerhook], #tf.train.NanTensorHook(loss),
			config = tf.ConfigProto(
					log_device_placement=False),
			save_summaries_steps = None, #self.SUMMARY_STEPS,
			save_summaries_secs = None) as mon_sess:

			test_writer = tf.summary.FileWriter(self.TEST_DIR, mon_sess.graph)
			train_writer = FileWriterCache.get(self.TRAIN_DIR) # SummaryWriterCache
			train_writer.add_summary(layout_summary)

			if restore_checkpoint and ckpt and ckpt.model_checkpoint_path:
				print("Restoring checkpoint...")
				pretrained_saver.restore(mon_sess, ckpt.model_checkpoint_path)

			while not mon_sess.should_stop():
				if (step % self.SUMMARY_STEPS == 0):
					_, merged, step = mon_sess.run([train_op, inception_summaries, global_step], feed_dict={it_handle:loggerhook.train_iterator_handle,
																											training_pl:True})
					train_writer.add_summary(merged, step)
				else:
					_, step = mon_sess.run([train_op, global_step], feed_dict={it_handle:loggerhook.train_iterator_handle,
																										training_pl:True})
				if (step % self.TEST_FREQUENCY == 0):
					print("Validation testing...")
					mon_sess.run(stream_vars_reset, feed_dict={it_handle:loggerhook.test_iterator_handle,
															   training_pl:False})
					while True:
						try:
							_, val_acc = mon_sess.run([validation_loss_update, validation_loss], feed_dict={it_handle:loggerhook.test_iterator_handle,
																											training_pl:False})
						except tf.errors.OutOfRangeError:
							break
					summ = mon_sess.run(valid_summ)
					test_writer.add_summary(summ, step)
					print("Validation loss: {}".format(val_acc))
					mon_sess.run(test_it.initializer, feed_dict={it_handle:loggerhook.test_iterator_handle})
					loggerhook._start_time = time.time()

	def retrain_from_pkl(self, model, weights):
		if model == None: model = '/home/shawarma/thyroid/models/inception_v4_2018_04_27/inception_v4.pb'
		if weights == None: weights = '/home/shawarma/thyroid/thyroid/obj/inception_v4_imagenet_pretrained.pkl'
		with open(weights, 'rb') as f:
			var_dict = pickle.load(f)
		self.train(retrain_model = model, retrain_weights = None)

if __name__ == "__main__":
	os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
	tf.logging.set_verbosity(tf.logging.ERROR)

	parser = argparse.ArgumentParser(description = "Train a CNN using an Inception-v4 network")
	parser.add_argument('-d', '--dir', help='Path to root directory for saving model.')
	parser.add_argument('-i', '--input', help='Path to root directory with training and eval data.')
	parser.add_argument('-r', '--retrain', help='Path to directory containing model to use as pretraining')
	args = parser.parse_args()

	SFM = SlideflowModel(args.dir, args.input)
	SFM.train(restore_checkpoint = args.retrain)