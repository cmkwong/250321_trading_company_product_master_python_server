from codes import config

from openai import OpenAI

import pandas as pd
import numpy as np

from glob import glob
from tqdm.notebook import tqdm

import matplotlib.pyplot as plt
from PIL import Image
import re
import os

import pytesseract
import pyperclip
class AIToolsController:

    def __init__(self):
        self.IMAGE_FOR_TXT_PATH = './docs/imageForTxt'
        # create the client for Deepseek
        self.client = OpenAI(api_key=config.DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
        # for image parse into text function
        pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_EXE_PATH

        # message history
        self.messages = []

    # clear the message history
    def clear_chat(self):
        self.messages = []

    def get_simple_response(self, question):
        response = self.client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "user", "content": question},
            ],
            stream=False
        )
        return response

    def get_system_message(self, user_message):
        if isinstance(user_message, str):
            self.messages.append({"role": "user", "content": user_message})
        response = self.client.chat.completions.create(
            model="deepseek-chat",
            messages=self.messages,
            stream=False
        )
        system_message = response.choices[0].message.content
        self.messages.append({"role": "system", "content": system_message})
        return system_message

    # style the email content
    def style_email(self, content, tone):
        question = f"This is email content:\n '{content}'. \nCould you please give us {tone} content. "
        response = self.get_simple_response(question)
        return response.choices[0].message.content

    def write_email(self, emailNote, toT):
        points_txt = '\n'.join([f"{i+1}. {p}" for i, p in enumerate(emailNote)])
        question = f"Help me write an {toT} email and please notice on below points: \n{points_txt}"
        response = self.get_simple_response(question)
        email_txt = response.choices[0].message.content
        pyperclip.copy(email_txt)
        return email_txt

    # style the content
    def style_content(self, content, action="professional and simple"):
        question = f"This is content '{content}'. Please {action} this content"
        response = self.get_simple_response(question)
        # print(response.choices[0].message.content)
        res_txt = ''
        for k in response.choices[0].message.content.split('\n')[2:-2]:
            k = re.sub(r'[\d]+\. ', '', k)
            k = re.sub(r'\*', '', k)
            k = k.strip()
            res_txt += f"{k}\n"
        return  res_txt

    # translate the content
    def translate_content(self, content, fromT, toT, tone):
        question = f"""
        This is content:
            '{content}'
        Please translate this content from {fromT} to {toT} in tone {tone}. 
        When you see the \\n, it is represented new point. 
        """
        response = self.get_simple_response(question)
        translated_txt = response.choices[0].message.content
        pyperclip.copy(translated_txt)
        print(translated_txt)
        return translated_txt

    def translate_content_simple(self, content, fromT, toT):
        question = f"""
        Translate below content from {fromT} to {toT}:
            '{content}'
        """
        response = self.get_simple_response(question)
        translated_txt = response.choices[0].message.content
        pyperclip.copy(translated_txt)
        print(translated_txt)
        return translated_txt

    # translate the product name
    def translate_product_name(self, content, fromT, toT, tone):
        question = f"""
        This is product name '{content}' in language {fromT}. Firstly, please translate into {toT}. Secondly, using this {toT} content to give product name with tone {tone}.
        The translation should be capitalized for first charactor in each word.
        """
        response = self.get_simple_response(question)
        product_name = response.choices[0].message.content.replace(',', '')
        pyperclip.copy(product_name)
        print(product_name)
        return product_name

    # generate the product keywords
    def generate_product_keywords(self, content):
        question = f"This is product name '{content}'. Please give me this product propular, distinct and different key words based on popular SEO"
        response = self.get_simple_response(question)
        keywords = ''
        for k in response.choices[0].message.content.split('\n')[2:-2]:
            k = re.sub(r'[\d]+\. ', '', k)
            k = re.sub(r'\*', '', k)
            k = k.strip()
            keywords += k + '\n'
        return keywords

    # generate product description base on product name
    def generate_product_description(self, content, tone):
        question = f"This is product name: '{content}'. \nCould you please give us {tone} description. "
        response = self.get_simple_response(question)
        description = response.choices[0].message.content.replace('**', '')
        pyperclip.copy(description)
        return description

    # transfer image into text
    def image_to_txt(self, filename, lang='chi_sim'):
        txt = pytesseract.image_to_string(os.path.join(self.IMAGE_FOR_TXT_PATH, filename), lang=lang).replace('\n', '').replace(' ', '')
        pyperclip.copy(txt)
        print(txt)
        return txt







