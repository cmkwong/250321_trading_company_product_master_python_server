import os
import pyperclip
import pandas as pd

from codes import config
from codes.utils import fileModel

class FileController:

    def __init__(self):
        self.SEO_FOLDER_PATH = config.ALIBABA_SEO_WORD_FOLDER_PATH

    def create_product_folder(self, folderName):
        productPath = os.path.join(config.PRODUCT_FOLDER_PATH, folderName)
        fileModel.createDir(productPath, 'display')
        fileModel.createDir(productPath, 'description')
        fileModel.createDir(productPath, 'video')
        fileModel.createDir(productPath, 'edited/display')
        fileModel.createDir(productPath, 'edited/description')
        pyperclip.copy(productPath)
        return folderName

    def readSEOWords_Alibaba(self, filename):
        try:
            seo_df = pd.read_excel(os.path.join(self.SEO_FOLDER_PATH, filename), sheet_name='行业热门词', header=5)
            seo_list = []
            for i, row in seo_df.iterrows():
                seo_list.append(f"{i + 1}. {row['关键词']}")
            seo_txt = "\n".join(seo_list)
        except Exception as e:
            return ''
        return seo_txt