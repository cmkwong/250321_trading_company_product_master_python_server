import os

import pandas as pd

from apps.core import config
from apps.core.clipboard import copy
from codes.utils import fileModel


class FileController:
    """Product-folder and SEO-keyword file service."""

    def __init__(self):
        self.SEO_FOLDER_PATH = config.get("ALIBABA_SEO_WORD_FOLDER_PATH")

    def create_product_folder(self, folderName):
        productPath = os.path.join(config.get("PRODUCT_FOLDER_PATH"), folderName)
        fileModel.createDir(productPath, "AMZ/display")
        fileModel.createDir(productPath, "AMZ/description")
        fileModel.createDir(productPath, "AMZ/video")
        fileModel.createDir(productPath, "Alibaba/display")
        fileModel.createDir(productPath, "Alibaba/description")
        fileModel.createDir(productPath, "Alibaba/video")
        fileModel.createDir(productPath, "Temu/display")
        fileModel.createDir(productPath, "Temu/description")
        fileModel.createDir(productPath, "Temu/video")
        fileModel.createDir(productPath, "IG/display")
        fileModel.createDir(productPath, "IG/description")
        fileModel.createDir(productPath, "IG/video")
        fileModel.createDir(productPath, "display")
        fileModel.createDir(productPath, "description")
        fileModel.createDir(productPath, "video")
        fileModel.createDir(productPath, "edited/display")
        fileModel.createDir(productPath, "edited/description")
        copy(productPath)
        return folderName

    def readSEOWords_Alibaba(self, filename):
        try:
            seo_df = pd.read_excel(
                os.path.join(self.SEO_FOLDER_PATH, filename),
                sheet_name="行业热门词",
                header=5,
            )
            seo_list = []
            for i, row in seo_df.iterrows():
                seo_list.append(f"{i + 1}. {row['关键词']}")
            seo_txt = "\n".join(seo_list)
        except Exception as e:
            return ""
        return seo_txt
