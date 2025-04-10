from datetime import datetime
# import pyscreenshot as ImageGrab
from PIL import ImageGrab
import os

class CapScreenController:
    def __init__(self):
        pass

    def cap(self, coord_tl, coord_rb, path):
        now = datetime.now()
        now_str = now.strftime('%Y%m%d_%H%M%S')
        im = ImageGrab.grab(bbox=(coord_tl[0], coord_tl[1], coord_rb[0], coord_rb[1]), include_layered_windows=False, all_screens=True)
        img_name = f'{now_str}.png'
        im.save(os.path.join(path, img_name))
        return img_name