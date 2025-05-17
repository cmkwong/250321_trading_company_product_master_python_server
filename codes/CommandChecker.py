import os
import sys

from functools import wraps
import pandas as pd
from codes import config
from codes.utils import paramModel, timeModel, fileModel, inputModel
from codes.utils.paramModel import command_check, params_check
from codes.controllers.PyputController import PyputController
from codes.controllers.CapScreenController import CapScreenController
from codes.controllers.ChatController import ChatController
from codes.controllers.FileController import FileController
from codes.controllers.WebController import WebController
from codes.controllers.ImageController import ImageController
from codes.controllers.PyautoController import PyautoController

class CommandChecker:


    def __init__(self):
        self.pyputController = PyputController()
        self.capScreenController = CapScreenController()
        self.chatController = ChatController()
        self.fileController = FileController()
        self.webController = WebController()
        self.imageController = ImageController()
        self.pyautoController = PyautoController()

        # constants
        self.COMMAND_CHECKED = 'CHECKED'
        self.COMMAND_NOT_CHECKED = 'NOT_CHECKED'
        self.ans = None  # being passed the command what user input
        self.COMMAND_HIT = False

        # variables
        self.Product_Index = ''

        # control variable
        self.QUIT = "quit"
        self.COMMAND_MODE = True

    @command_check([''])
    def empty_command(self):
        print("cannot input empty string")
        return self.COMMAND_CHECKED

    @command_check(['q'])
    def quit(self):
        return self.QUIT

    @command_check(['ask'])
    @params_check({
        'question': ['', str]
    })
    def ask_question(self, **params):
        response = self.chatController.get_simple_response(**params)
        system_message = response.choices[0].message.content
        print(system_message)
        return self.COMMAND_CHECKED

    # capture the text and paste it onto translate.txt
    @command_check(['ct', 'capt'])
    def capture_txt(self):
        print("First coordinate. ")
        firstCoord = self.pyputController.get_pos_by_leftClick()
        print("Second coordinate. ")
        secondCoord = self.pyputController.get_pos_by_leftClick()
        # cap screen
        filename = self.capScreenController.cap(firstCoord, secondCoord, path=config.IMAGE_TO_TEXT_PATH)
        # transfer into txt
        txt = self.chatController.image_to_txt(filename)
        fileModel.write_txt(config.IMAGE_TO_TEXT_PATH, 'transfer.txt', f"{txt}\n", 'a')
        return self.COMMAND_CHECKED

    # translate the text from translate.txt file
    @command_check(['tl'])
    def translate_text_file(self):
        txt = fileModel.read_text(config.IMAGE_TO_TEXT_PATH, 'transfer.txt')
        translated_txt = self.chatController.translate_content(txt, 'Chinese', "English", "Simple Professional")
        fileModel.write_txt(config.IMAGE_TO_TEXT_PATH, "result.txt", f"{translated_txt}\n", 'a')
        return self.COMMAND_CHECKED

    # translate the product name
    @command_check(['tln'])
    @params_check({
        'content': ['', str],
        'fromT': ['Chinese', str],
        'toT': ['English', str],
        'tone': ['Simple and Professional', str],
        'seo_filename': ['', str]
    })
    def translate_product_name(self, **params):
        seo_txt = self.fileController.readSEOWords_Alibaba(params['seo_filename'])
        translated_txt = self.chatController.translate_product_name(params['content'], params['fromT'], params['toT'], params['tone'], seo_txt)
        return self.COMMAND_CHECKED

    # simple translate
    @command_check(['t'])
    def translate_user_text(self):
        print("Input the translate txt: ")
        txt = inputModel.txt_input()
        translated_txt = self.chatController.translate_content(txt, 'Chinese', "English", "Simple Professional")
        return self.COMMAND_CHECKED

    # capture and translate the text
    @command_check(['ctt', 'captt'])
    def capture_txt_translate(self):
        print("First coordinate. ")
        firstCoord = self.pyputController.get_pos_by_leftClick()
        print("Second coordinate. ")
        secondCoord = self.pyputController.get_pos_by_leftClick()
        # cap screen
        filename = self.capScreenController.cap(firstCoord, secondCoord, path=config.IMAGE_TO_TEXT_PATH)
        # transfer into txt
        txt = self.chatController.image_to_txt(filename)
        translated_txt = self.chatController.translate_content(txt, 'Chinese', "English", "Simple Professional")
        fileModel.write_txt(config.IMAGE_TO_TEXT_PATH, "result.txt", f"{translated_txt}\n", 'a')
        return self.COMMAND_CHECKED

    @command_check(['pos'])
    def get_pos(self):
        coord = self.pyputController.get_pos_by_leftClick()
        print(coord)
        return self.COMMAND_CHECKED

    @command_check(['kw'])
    @params_check({
        'content': ['', str]
    })
    def get_product_keyword(self, **params):
        keywords = self.chatController.generate_product_keywords(**params)
        print(keywords)
        return self.COMMAND_CHECKED

    @command_check(['des'])
    @params_check({
        'content': ['', str],
        'tone': ['business and professional', str]
    })
    def get_product_description(self, **params):
        description = self.chatController.generate_product_description(**params)
        print(description)
        return self.COMMAND_CHECKED

    # list out what you want in the email
    @command_check(['we'])
    @params_check({
        'emailNote': [[], list],
        'toT': ['English', str]
    })
    def write_email(self, **params):
        email_txt = self.chatController.write_email(**params)
        fileModel.write_txt(config.IMAGE_TO_TEXT_PATH, "result.txt", f"----\n{email_txt}\n", 'a')
        print(email_txt)
        return self.COMMAND_CHECKED

    # create product folder
    @command_check(['pf'])
    @params_check({
        'folderName': ['', str]
    })
    def create_product_folder(self, **params):
        self.Product_Index = self.fileController.create_product_folder(**params)
        return self.COMMAND_CHECKED

    # download 1688 images and video
    @command_check(['d1688'])
    @params_check({
        'website': ['', str],
        'product_id': ['', str],
        'quit_finally': [True, bool]
    })
    def download_1688_src(self, **params):
        self.webController.download_1688_images(**params)
        return self.COMMAND_CHECKED

    # download 1688 images and video from html text
    @command_check(['t1688'])
    @params_check({
        'product_id': ['', str]
    })
    def download_1688_src_text(self, **params):
        # html = fileModel.read_text(config.DOCS, "html.txt")
        self.webController.download_1688_images_from_html(params['product_id'])
        return self.COMMAND_CHECKED

    @command_check(['re'])
    def reopen_driver(self):
        self.webController.init_driver()
        return self.COMMAND_CHECKED

    # extract image text from deepseek API
    @command_check(['tf'])
    @params_check({
        'product_index': [r'', str]
    })
    def extract_texts(self, **params):
        # read display folder
        self.imageController.write_extracted_txt(params['product_index'], 'display')
        # read description folder
        self.imageController.write_extracted_txt(params['product_index'], 'description')
        return self.COMMAND_CHECKED

    @command_check(['bc'])
    @params_check({
        "product_index": ['', str],
        "imagesType": ['', str]
    })
    def buildcanva(self, **params):
        self.pyautoController.product_into_canva_attempts(params['product_index'], params['imagesType'])
        return self.COMMAND_CHECKED

    @command_check(['bcs'])
    @params_check({
        "product_indexs": ['', list]
    })
    def buildcanvas(self, **params):
        for product_index in params['product_indexs']:
            self.pyautoController.product_into_canva_attempts(product_index, 'display')
            self.pyautoController.product_into_canva_attempts(product_index, 'description')
        return self.COMMAND_CHECKED

    @command_check()
    def bbox(self):
        self.pyautoController.get_bbox_from_clicks()
        return self.COMMAND_CHECKED

    @command_check()
    def zip(self):
        product_ids = [os.path.join(config.PRODUCT_FOLDER_PATH, t.strip()) for t in fileModel.read_text(r'./docs', 'zip_to_agent_product_id.txt').split('\n') if len(t.strip()) > 0]
        fileModel.zip_folders_combined(product_ids, os.path.join(config.PRODUCT_FOLDER_PATH, f"PetProductImages_{timeModel.get_time_str('%Y%m%d')}.zip"))
        return self.COMMAND_CHECKED


