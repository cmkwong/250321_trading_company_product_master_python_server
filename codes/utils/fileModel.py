import os
from codes.utils import listModel
from urllib.parse import urlparse
from pathlib import Path
import zipfile


def getFileList(pathDir, reverse=False, filter_temp=True, extensions=None):
    """
    Get a sorted list of files in a directory with various filtering options.

    Args:
        pathDir (str): Path to the directory
        reverse (bool): Sort in reverse order
        filter_temp (bool): Whether to filter temporary files (starting with '~' or '.')
        extensions (list): Optional list of allowed file extensions (e.g., ['.txt', '.jpg'])

    Returns:
        list: Sorted list of filenames
    """
    try:
        # Get all entries in directory
        entries = os.listdir(pathDir)

        # Filter files
        filtered_files = []
        for entry in entries:
            # Skip if it's a directory
            if not os.path.isfile(os.path.join(pathDir, entry)):
                continue

            # Filter temporary files if enabled
            if filter_temp and (entry.startswith('~') or entry.startswith('.')):
                continue

            # Filter by extension if specified
            if extensions:
                ext = os.path.splitext(entry)[1].lower()
                if ext not in extensions:
                    continue

            filtered_files.append(entry)

        # Sort files (case-insensitive)
        return sorted(filtered_files, key=str.lower, reverse=reverse)

    except FileNotFoundError:
        print(f"Directory not found: {pathDir}")
        return []
    except PermissionError:
        print(f"Permission denied for directory: {pathDir}")
        return []
    except Exception as e:
        print(f"Error reading directory {pathDir}: {str(e)}")
        return []

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
    Creates a directory if it doesn't exist, and optionally adds a readme.txt file.

    Parameters:
        main_path (str): The parent directory path
        dir_name (str): The name of the directory to create
        readme (bool/str): If True, creates empty readme.txt. If string, uses as content.

    Returns:
        str: Full path of the created directory
    """
    # Create the full directory path
    full_path = os.path.join(main_path, dir_name)

    try:
        # Create directory (exist_ok prevents error if directory exists)
        os.makedirs(full_path, exist_ok=True)
        print(f"Directory '{full_path}' created or already exists")

        # Create readme file if requested
        if readme:
            readme_path = os.path.join(full_path, "readme.txt")
            with open(readme_path, 'w') as f:
                if isinstance(readme, str):
                    f.write(readme)
                else:
                    f.write("")  # Empty file
            print(f"Created readme.txt in '{full_path}'")

        return full_path

    except Exception as e:
        print(f"Error creating directory: {e}")
        return None

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

def getFileExt(fileName):
    file_ext = Path(urlparse(fileName).path).suffix
    return file_ext

def zip_folders_combined(folder_paths, output_path):
    """
    Combines multiple folders into a single zip file.

    Args:
        folder_paths (list): List of paths to folders to be zipped
        output_path (str): Path for the output zip file (including .zip extension)

    Returns:
        str: Path to created zip file

    Example:
        zip_folders_combined(['/path/folder1', '/path/folder2'], '/output/combined.zip')
    """
    output_path = Path(output_path).resolve()

    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for folder_path in folder_paths:
            folder = Path(folder_path).resolve()

            # Walk through each file in the folder
            for root, dirs, files in os.walk(folder):
                for file in files:
                    file_path = Path(root) / file

                    # Create relative path for zip file
                    rel_path = file_path.relative_to(folder.parent)

                    # Add file to zip
                    zipf.write(file_path, rel_path)
    print(f"{len(folder_paths)} files is stored into {output_path}")
    return str(output_path)