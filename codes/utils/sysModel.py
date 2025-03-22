import os

# get the parent path
def getTargetPath(targetName):
    currentPath = os.getcwd()
    while True:
        if (os.path.basename(currentPath) == targetName):
            return currentPath
        base = os.path.basename(os.path.abspath(os.path.join(currentPath, os.pardir)))
        currentPath = os.path.dirname(currentPath)
        if base == targetName:
            return currentPath
        if len(base) == 0:
            raise Exception("No such target name found. ")
