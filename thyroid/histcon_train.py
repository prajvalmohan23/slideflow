# Copyright (C) James Dolezal - All Rights Reserved
# Unauthorized copying of this file, via any medium is strictly prohibited
# Proprietary and confidential
# Written by James Dolezal <jamesmdolezal@gmail.com>, October 2017
# ==========================================================================

"""Builds the HISTCON network."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from datetime import datetime
import time

import tensorflow as tf

import histcon
#import tf_cnnvis
import inception_v4

parser = histcon.parser

def train():
	'''Train HISTCON for a number of steps.'''
	with tf.Graph().as_default():
		global_step = tf.train.get_or_create_global_step()

		# Get images and labels.
		# Force input pipeline to CPU:0 to avoid operations ending up on GPU.
		with tf.device('/cpu:0'):
			images, labels = histcon.processed_inputs()

		# Build a Graph that computes the logits predictions from
		# the inference model.
		#logits = histcon.inference(images)
		logits, end_points = inception_v4.inception_v4(images, num_classes=histcon.NUM_CLASSES)

		# Calculate the loss.
		loss = histcon.loss(logits, labels)

		# Build a Graph that trains the model with one batch of
		# examples and updates the model parameters.
		train_op = histcon.train(loss, global_step)

		# Visualize CNN activations
		#tf_cnnvis.deconv_visualization(tf.get_default_graph(), None, input_tensor = images)

		class _LoggerHook(tf.train.SessionRunHook):
			'''Logs loss and runtime.'''

			def begin(self):
				self._step = -1
				self._start_time = time.time()

			def before_run(self, run_context):
				self._step += 1
				return tf.train.SessionRunArgs(loss) # Asks for loss value.

			def after_run(self, run_context, run_values):
				if self._step % FLAGS.log_frequency == 0:
					current_time = time.time()
					duration = current_time - self._start_time
					self._start_time = current_time

					loss_value = run_values.results
					images_per_sec = FLAGS.log_frequency * FLAGS.batch_size / duration
					sec_per_batch = float(duration / FLAGS.log_frequency)

					format_str = ('%s: step %d, loss = %.2f (%.1f images/sec; %.3f sec/batch)')
					print(format_str % (datetime.now(), self._step, loss_value,
										images_per_sec, sec_per_batch))

		with tf.train.MonitoredTrainingSession(
			checkpoint_dir = FLAGS.model_dir,
			hooks = [tf.train.StopAtStepHook(last_step = FLAGS.max_steps),
					tf.train.NanTensorHook(loss),
					_LoggerHook()],
			config = tf.ConfigProto(
					log_device_placement=False),
			save_summaries_steps = FLAGS.summary_steps) as mon_sess:
			while not mon_sess.should_stop():
				mon_sess.run(train_op)

def main(argv=None):
	if tf.gfile.Exists(FLAGS.model_dir):
		tf.gfile.DeleteRecursively(FLAGS.model_dir)
	tf.gfile.MakeDirs(FLAGS.model_dir)
	train()

if __name__ == "__main__":
	FLAGS = parser.parse_args()
	tf.app.run()