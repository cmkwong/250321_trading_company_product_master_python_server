import pandas as pd
import os
from codes.utils import listModel

def getFileList(pathDir, reverse=False):
    required_fileNames = []
    listFiles = os.listdir(pathDir)
    for fileName in listFiles:
        if fileName[0] != '~': # discard the temp file
            required_fileNames.append(fileName)
    required_fileNames = sorted(required_fileNames, reverse=reverse)
    return required_fileNames

def clearFiles(pathDir, pattern=None):
    """
    pattern None means clear all files in the pathDir
    """
    files = getFileList(pathDir)
    if pattern:
        files = listModel.filterList(files, pattern)
    for file in files:
        os.remove(os.path.join(pathDir, file))
        print("The file {} has been removed.".format(file))

def createDir(main_path, dir_name, readme=None):
    """
    Create directory with readme.txt
    """
    path = os.path.join(main_path, dir_name)
    if not os.path.isfile(path):
        os.mkdir(path)
    if readme:
        with open(os.path.join(path, 'readme.txt'), 'a') as f:
            f.write(readme)

# reading txt
def read_text(main_path, file_name):
    with open(os.path.join(main_path, file_name), 'r', encoding='UTF-8') as f:
        txt = f.read()
    return txt

def readAllTxtFiles(fileDir, outFormat=dict, deep=True):
    """
    :param fileDir: str
    :return: {}
    """
    output = outFormat()    # define the init data type
    for d, (curPath, directories, files) in enumerate(os.walk(fileDir)):    # deep walk
        # if not deep, then only read first level
        if not deep and d > 0:
            break
        for file in files:
            with open(os.path.join(curPath, file), 'r', encoding='UTF-8') as f:
                if outFormat == dict:
                    output[file] = f.read()
                elif outFormat == str:
                    output += f.read() + '\n'
    return output

def write_txt(main_path, filename, txt, method):
    with open(os.path.join(main_path, filename), method, encoding='UTF-8') as f:
        f.write(txt)
    print("Written {}".format(filename))

def writeAllTxtFiles(main_path, texts, method='w'):
    """
    :param texts: dic
    :param path: str
    :return:
    """
    for filename, txt in texts.items():
        if filename[0] != '_':
            write_txt(main_path, filename, txt, method)