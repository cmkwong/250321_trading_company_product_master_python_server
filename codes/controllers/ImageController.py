# import os
# import matplotlib.pyplot as plt
# import keras_ocr
# import cv2
# import math
# import numpy as np
# os.environ['TF_ENABLE_ONEDNN_OPTS'] = '0'
# pipeline = keras_ocr.pipeline.Pipeline()
# class ImageController:
#     def __init__(self):
#         # self.pipeline = keras_ocr.pipeline.Pipeline()
#         pass
#
#     def midpoint(self, x1, y1, x2, y2):
#         x_mid = int( (x1 + x2) / 2 )
#         y_mid = int( (y1 + y2) / 2)
#         return (x_mid, y_mid)
#
#     def inpaint_text(self, folderPath: str, filename: str):
#         # read image
#         fullPath = os.path.join(folderPath, filename)
#         img =keras_ocr.tools.read(fullPath)
#         # generate (word, box) tuples
#         prediction_groups = pipeline.recognize(([img]))
#         mask = np.zeros(img.shape[:2], dtype='uint8')
#         for box in prediction_groups[0]:
#             x0, y0 = box[1][0]
#             x1, y1 = box[1][1]
#             x2, y2 = box[1][2]
#             x3, y3 = box[1][3]
#             # finding the mid-points
#             x_mid0, y_mid0 = self.midpoint(x1, y1, x2, y2)
#             x_mid1, y_mid1 = self.midpoint(x0, y0, x3, y3)
#             # calculate thickness
#             thickness = int(math.sqrt( (x2 - x1)**2) + ((y2 - y1)**2) )
#
#             cv2.line(mask, (x_mid0, y_mid0), (x_mid1, y_mid1), 255, thickness)
#
#             img = cv2.inpaint(img, mask, 7, cv2.INPAINT_NS)
#
#         return img
#
