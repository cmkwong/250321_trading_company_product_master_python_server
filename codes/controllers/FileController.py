import os
import pyperclip

from codes import config
from codes.utils import fileModel
class FileController:

    def __init__(self):
        pass

    def create_product_folder(self, folderName):
        productPath = os.path.join(config.PRODUCT_FOLDER_PATH, folderName)
        fileModel.createDir(productPath, 'display')
        fileModel.createDir(productPath, 'description')
        fileModel.createDir(productPath, 'video')
        pyperclip.copy(productPath)
        return True