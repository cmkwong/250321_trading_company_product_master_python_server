import os
import sys

from functools import wraps
import pandas as pd
from codes import config
from codes.utils import paramModel, timeModel, fileModel, inputModel
from codes.utils.paramModel import command_check, params_check
from codes.controllers.PyputController import PyputController
from codes.controllers.CapScreenController import CapScreenController
from codes.controllers.AIToolsController import AIToolsController
# from codes.controllers.ImageController import ImageController

class CommandChecker:

    def __init__(self):
        self.systemController = PyputController()
        self.capScreenController = CapScreenController()
        self.aiToolsController = AIToolsController()
        # self.imageController = ImageController()

        # constants
        self.COMMAND_CHECKED = 'CHECKED'
        self.COMMAND_NOT_CHECKED = 'NOT_CHECKED'
        self.ans = None  # being passed the command what user input
        self.COMMAND_HIT = False

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

    @command_check(['ct', 'capt'])
    def capture_txt(self):
        print("First coordinate. ")
        firstCoord = self.systemController.get_click_pos()
        print("Second coordinate. ")
        secondCoord = self.systemController.get_click_pos()
        # cap screen
        filename = self.capScreenController.cap(firstCoord, secondCoord, path=config.IMAGE_TO_TEXT_PATH)
        # transfer into txt
        txt = self.aiToolsController.image_to_txt(filename)
        fileModel.write_txt(config.IMAGE_TO_TEXT_PATH, 'transfer.txt', f"{txt}\n", 'a')
        return self.COMMAND_CHECKED

    @command_check(['tl'])
    def translate_text_file(self):
        txt = fileModel.read_text(config.IMAGE_TO_TEXT_PATH, 'transfer.txt')
        translated_txt = self.aiToolsController.translate_content(txt, 'Chinese', "English", "Simple Professional")
        fileModel.write_txt(config.IMAGE_TO_TEXT_PATH, "result.txt", f"{translated_txt}\n", 'a')
        return self.COMMAND_CHECKED

    @command_check(['tln'])
    @params_check({
        'content': ['', str],
        'fromT': ['Chinese', str],
        'toT': ['English', str],
        'tone': ['Simple and Professional', str],
    })
    def translate_product_name(self, **params):
        translated_txt = self.aiToolsController.translate_product_name(**params)
        return self.COMMAND_CHECKED

    @command_check(['t'])
    def translate_user_text(self):
        print("Input the translate txt: ")
        txt = inputModel.txt_input()
        translated_txt = self.aiToolsController.translate_content(txt, 'Chinese', "English", "Simple Professional")
        return self.COMMAND_CHECKED

    @command_check(['ctt', 'captt'])
    def capture_txt_translate(self):
        print("First coordinate. ")
        firstCoord = self.systemController.get_click_pos()
        print("Second coordinate. ")
        secondCoord = self.systemController.get_click_pos()
        # cap screen
        filename = self.capScreenController.cap(firstCoord, secondCoord, path=config.IMAGE_TO_TEXT_PATH)
        # transfer into txt
        txt = self.aiToolsController.image_to_txt(filename)
        translated_txt = self.aiToolsController.translate_content(txt, 'Chinese', "English", "Simple Professional")
        fileModel.write_txt(config.IMAGE_TO_TEXT_PATH, "result.txt", f"{translated_txt}\n", 'a')
        return self.COMMAND_CHECKED

    @command_check(['pos'])
    def get_pos(self):
        coord = self.systemController.get_click_pos()
        print(coord)
        return self.COMMAND_CHECKED

    @command_check(['kw'])
    @params_check({
        'content': ['', str]
    })
    def get_product_keyword(self, **params):
        keywords = self.aiToolsController.generate_product_keywords(**params)
        print(keywords)
        return self.COMMAND_CHECKED

    @command_check(['des'])
    @params_check({
        'content': ['', str],
        'tone': ['business and professional', str]
    })
    def get_product_description(self, **params):
        description = self.aiToolsController.generate_product_description(**params)
        print(description)
        return self.COMMAND_CHECKED

    @command_check(['we'])
    @params_check({
        'points': [{'1': 'test1', '2': 'test'}, dict],
        'toT': ['English', str]
    })
    def write_email(self, **params):
        email_txt = self.aiToolsController.write_email(**params)
        fileModel.write_txt(config.IMAGE_TO_TEXT_PATH, "result.txt", f"----\n{email_txt}\n", 'a')
        print(email_txt)
        return self.COMMAND_CHECKED

    # @command_check(['ipt'])
    # def remove_txt_from_image(self):
    #     self.imageController.inpaint_text(r"C:\Users\Chris\Desktop\StockData\Business\Pet Product Images\202504060939\raw", "O1CN017idd0T1KebeHTG5tY_!!2206724201189-0-cib.jpg")
