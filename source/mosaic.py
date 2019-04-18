import numpy as np
import matplotlib.pyplot as plt
import matplotlib.cm as cm
from matplotlib.patches import Rectangle

import argparse
import json
import sys
import math
import csv

import openslide as ops

import os
from os.path import join, isfile, exists

# TODO: May need to manually specify origin for overlaid rectangles

class Mosaic:
	def __init__(self, args):
		self.metadata = []
		self.tsne_points = [] # format: (x, y, index)
		self.tiles = []
		self.tile_point_distances = []
		self.rectangles = []
		self.tile_um = args.um
		self.SLIDES = {}
		self.num_tiles_x = 70
		self.stride_div = 1
		self.tile_root = args.tile
		self.export = args.export

		self.initiate_figure()
		self.load_metadata(args.meta)
		self.load_bookmark_state(args.bookmark)

	def generate(self):
		self.draw_slides()
		self.place_tile_outlines()
		self.generate_hover_events()
		self.calculate_distances()
		self.pair_tiles_and_points()
		self.finish_mosaic(self.export)

	def load_slides(self, slides_array, directory, category="None"):
		print(f"Loading SVS files from {directory} ...")
		for slide in slides_array:
			name = slide[:-4]
			filetype = slide[-3:]
			path = slide if not directory else join(directory, slide)

			try:
				slide = ops.OpenSlide(path)
			except ops.lowlevel.OpenSlideUnsupportedFormatError:
				print(f"Unable to read file from {path} , skipping")
				return None
	
			shape = slide.dimensions
			goal_thumb_area = 800*800
			y_x_ratio = shape[1] / shape[0]
			thumb_x = math.sqrt(goal_thumb_area / y_x_ratio)
			thumb_y = thumb_x * y_x_ratio
			thumb_ratio = thumb_x / shape[0]
			thumb = slide.get_thumbnail((int(thumb_x), int(thumb_y)))
			MPP = float(slide.properties[ops.PROPERTY_NAME_MPP_X]) # Microns per pixel

			# Calculate tile index -> cooordinates dictionary
			coords = []
			extract_px = int(self.tile_um / MPP)
			stride = int(extract_px / self.stride_div)
			for y in range(0, (shape[1]+1) - extract_px, stride):
				for x in range(0, (shape[0]+1) - extract_px, stride):
					if ((y % extract_px == 0) and (x % extract_px == 0)):
						# Indicates unique (non-overlapping tile)
						coords.append([x, y])

			self.SLIDES.update({name: { "name": name,
										"path": path,
										"type": filetype,
										"category": category,
										"thumb": thumb,
										"ratio": thumb_ratio,
										"MPP": MPP,
										'coords':coords} })

	def draw_slides(self):
		print("Drawing slides...")
		self.ax_thumbnail = self.fig.add_subplot(122)
		self.ax_thumbnail.set_xticklabels([])
		self.ax_thumbnail.set_yticklabels([])
		name = list(self.SLIDES)[0]
		self.ax_thumbnail.imshow(self.SLIDES[name]['thumb'])
		self.fig.canvas.draw()
		self.svs_background = self.fig.canvas.copy_from_bbox(self.ax_thumbnail.bbox)
		self.SLIDES[name]['plot'] = self.ax_thumbnail

	def generate_hover_events(self):
		def hover(event):
			# Check if mouse hovering over scatter plot
			for rect in self.rectangles:
				rect.remove()
			self.rectangles = []
			if self.tsne_plot.contains(event)[0]:
				self.fig.canvas.restore_region(self.svs_background)
				indices = self.tsne_plot.contains(event)[1]["ind"]
				for index in indices:
					point = self.tsne_points[index]
					case = point['case']
					if case in self.SLIDES:
						slide = self.SLIDES[case]
						tile_extracted_px = int(self.tile_um / slide['MPP'])
						size = slide['ratio']*tile_extracted_px
						origin_x, origin_y = slide['coords'][point['tile_num']]
						origin_x *= slide['ratio']
						origin_y *= slide['ratio']
						tile_outline = Rectangle((origin_x,# - size/2, 
										  		  origin_y),# - size/2), 
										  		  size, 
										  		  size, 
										  		  fill=None, alpha=1, color='green',
												  zorder=100)
						self.rectangles.append(tile_outline)
						self.ax_thumbnail.add_artist(tile_outline) #add_patch
						self.ax_thumbnail.draw_artist(tile_outline)
				self.fig.canvas.blit(self.ax_thumbnail.bbox)
		def resize(event):
			for rect in self.rectangles:
				rect.remove()
			self.fig.canvas.draw()
			self.svs_background = self.fig.canvas.copy_from_bbox(self.ax_thumbnail.bbox)

		self.fig.canvas.mpl_connect('motion_notify_event', hover)
		self.fig.canvas.mpl_connect('resize_event', resize)

	def initiate_figure(self):
		print("Initializing figure...")
		if self.export:
			self.fig = plt.figure(figsize=(200,200))
		else:
			self.fig = plt.figure(figsize=(24,18))
		self.ax = self.fig.add_subplot(121, aspect='equal')
		self.fig.tight_layout()
		plt.subplots_adjust(left=0.02, bottom=0, right=0.98, top=1, wspace=0.1, hspace=0)
		self.ax.set_aspect('equal', 'box')
		self.ax.set_xticklabels([])
		self.ax.set_yticklabels([])

	def load_metadata(self, path):
		print("Loading metadata...")
		with open(path, 'r') as metadata_file:
			reader = csv.reader(metadata_file, delimiter='\t')
			headers = next(reader, None)
			for row in reader:
				self.metadata.append(row)

	def load_bookmark_state(self, path):
		print("Loading t-SNE bookmark and plotting points...")
		with open(path, 'r') as bookmark_file:
			state = json.load(bookmark_file)
			projection_points = state[0]['projections']
			points_x = []
			points_y = []
			point_index = 0
			for i, p in enumerate(projection_points):
				meta = self.metadata[i]
				tile_num = int(meta[0])
				case = meta[1]
				category = meta[2]
				if 'tsne-1' in p:
					points_x.append(p['tsne-0'])
					points_y.append(p['tsne-1'])
					self.tsne_points.append({'x':p['tsne-0'],
										'y':p['tsne-1'],
										'index':point_index,
										'tile_num':tile_num,
										'neighbors':[],
										'category':category,
										'case':case,
										'paired_tile':None,
										'image_path':join(self.tile_root, case, f"{case}_{tile_num}.jpg")})
					point_index += 1
			x_points = [p['x'] for p in self.tsne_points]
			y_points = [p['y'] for p in self.tsne_points]
			_x_width = max(x_points) - min(x_points)
			_y_width = max(y_points) - min(y_points)
			buffer = (_x_width + _y_width)/2 * 0.05
			max_x = max(x_points) + buffer
			min_x = min(x_points) - buffer
			max_y = max(y_points) + buffer
			min_y = min(y_points) - buffer

		self.tsne_plot = self.ax.scatter(points_x, points_y, s=4000, facecolors='none', edgecolors='green', alpha=0)# markersize = 5
		self.tile_size = (max_x - min_x) / self.num_tiles_x
		self.num_tiles_y = int((max_y - min_y) / self.tile_size)
		self.max_distance = math.sqrt(2*((self.tile_size/2)**2))

		self.tile_coord_x = [(i*self.tile_size)+min_x for i in range(self.num_tiles_x)]
		self.tile_coord_y = [(j*self.tile_size)+min_y for j in range(self.num_tiles_y)]

	def place_tile_outlines(self):
		print("Placing tile outlines...")
		tile_index = 0
		for y in self.tile_coord_y:
			for x in self.tile_coord_x:
				tile = Rectangle((x - self.tile_size/2, 
								  y - self.tile_size/2), 
								self.tile_size, 
								self.tile_size, 
								fill=None, alpha=0, color='white')
				self.ax.add_patch(tile)
				self.tiles.append({'rectangle':tile,
							'x':x,
							'y':y,
							'index':tile_index,
							'neighbors':[],
							'paired_point':None})
				tile_index += 1

	def calculate_distances(self):
		print("Calculating tile-point distances...")
		for tile in self.tiles:
			# Calculate distance for each point from center
			distances = []
			for point in self.tsne_points:
				distance = math.sqrt((point['x']-tile['x'])**2 + (point['y']-tile['y'])**2)
				distances.append([point['index'], distance])
			distances.sort(key=lambda d: d[1])
			for d in distances:
				if d[1] <= self.max_distance:
					tile['neighbors'].append(d)
					self.tsne_points[d[0]]['neighbors'].append([tile['index'], d[1]])
					self.tile_point_distances.append({'distance': d[1],
												'tile_index':tile['index'],
												'point_index':d[0]})
				else:
					break
		self.tile_point_distances.sort(key=lambda d: d['distance'])

	def pair_tiles_and_points(self):
		print("Optimizing tile/point pairing...")
		num_placed = 0
		for distance_pair in self.tile_point_distances:
			# Attempt to place pair, skipping if unable (due to other prior pair)
			point = self.tsne_points[distance_pair['point_index']]
			tile = self.tiles[distance_pair['tile_index']]
			if not (point['paired_tile'] or tile['paired_point']):
				num_placed += 1
				point['paired_tile'] = True
				tile['paired_point'] = True

				tile_image = plt.imread(point['image_path'])
				self.ax.imshow(tile_image, aspect='equal', origin='lower', extent=[tile['x']-self.tile_size/2, 
																				   tile['x']+self.tile_size/2,
																				   tile['y']-self.tile_size/2,
																				   tile['y']+self.tile_size/2], zorder=99)		
		print(f"Num placed: {num_placed}")

	def finish_mosaic(self, export):
		print("Displaying/exporting figure...")
		self.ax.autoscale(enable=True, tight=True)
		if export:
			plt.savefig(join(self.tile_root, 'Mosaic_map.png'), bbox_inches='tight')
			plt.close()
		else:
			plt.show()

def get_args():
	parser = argparse.ArgumentParser(description = 'Creates a t-SNE histology tile mosaic using a saved t-SNE bookmark generated with Tensorboard.')
	parser.add_argument('-b', '--bookmark', help='Path to saved Tensorboard *.txt bookmark file.')
	parser.add_argument('-m', '--meta', help='Path to Tensorboard metadata.tsv file.')
	parser.add_argument('-t', '--tile', help='Path to root directory containing image tiles, separated in directories according to case name.')
	parser.add_argument('-s', '--slide', help='(Optional) Path to whole slide images (SVS or JPG format)')
	parser.add_argument('--um', type=float, help='(Necessary if plotting SVS) Size of extracted image tiles in microns.')
	parser.add_argument('--export', action="store_true", help='Save mosaic to png file.')
	return parser.parse_args()

if __name__ == '__main__':
	args = get_args()
	mosaic = Mosaic(args)

	if args.slide and not args.um:
		raise ValueError("Size of extracted tiles (in microns) must be supplied when viewing SVS files; please use the --um flag.")
	if args.slide and isfile(args.slide):
		# Load a single SVS
		slide_list = [args.slide.split('/')[-1]]
		slide_dir = "/".join(args.slide.split('/')[:-1])
		mosaic.load_slides(slide_list, slide_dir)
	elif args.slide:
		# First, load images in the directory, not assigning any category
		slide_list = [i for i in os.listdir(args.slide) if (isfile(join(args.slide, i)) and (i[-3:].lower() in ("svs", "jpg")))]	
		mosaic.load_slides(slide_list, args.slide)
		# Next, load images in subdirectories, assigning category by subdirectory name
		dir_list = [d for d in os.listdir(args.slide) if not isfile(join(args.slide, d))]
		for directory in dir_list:
			# Ignore images if in the thumbnails or QuPath project directory
			if directory in ["thumbs", "ignore", "QuPath_Project"]: continue
			slide_list = [i for i in os.listdir(join(args.slide, directory)) if (isfile(join(args.slide, directory, i)) and (i[-3:].lower() in ("svs", "jpg")))]	
			mosaic.load_slides(slide_list, join(args.slide, directory), category=directory)

	mosaic.generate()