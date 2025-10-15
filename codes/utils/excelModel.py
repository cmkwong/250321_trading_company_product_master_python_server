import pandas as pd
import os
from codes.utils import dicModel, listModel, dfModel, fileModel

def readExcel(path, required_sheets, concat=True, columnMapper=None):
    dfs = pd.read_excel(path, sheet_name=None, header=1)
    required_dfs = {}
    dfs = dicModel.changeCase(dfs, case='l')    # change sheet name of read excel case to lower
    required_sheets = listModel.changeCase(required_sheets, case='l')   # change required name case to lower
    for sheet_name in required_sheets:
        if sheet_name.lower() in dfs.keys():   # check if required sheet in excel file
            if columnMapper:
                # product_SEO_keywords column name, if exist
                dfs[sheet_name] = dfs[sheet_name].rename(columnMapper, axis='columns')
                # drop not required columns
                dfs[sheet_name] = dfs[sheet_name][dfs[sheet_name].columns.intersection(set(columnMapper.values()))]
            required_dfs[sheet_name] = dfs[sheet_name]
    if concat:
        return dfModel.concatDfs(required_dfs)
    return required_dfs

def xlsx2csv(main_path):
    """
    note 84d
    :param main_path: str, the xlsx files directory
    :return:
    """
    files = fileModel.getFileList(main_path, reverse=False)
    for file in files:
        # read excel file
        excel_full_path = os.path.join(main_path, file)
        print("Reading the {}".format(file))
        df = pd.read_excel(excel_full_path, header=None)

        # csv file name
        csv_file = file.split('.')[0] + '.csv'
        csv_full_path = os.path.join(main_path, csv_file)
        print("Writing the {}".format(csv_file))
        df.to_csv(csv_full_path, encoding='utf-8', index=False, header=False)
    return True

def transfer_all_xlsx_to_csv(main_path):
    """
    note 84d
    :param main_path: str, the xlsx files directory
    :return:
    """
    files = fileModel.getFileList(main_path, reverse=False)
    for file in files:
        # read excel file
        excel_full_path = os.path.join(main_path, file)
        print("Reading the {}".format(file))
        df = pd.read_excel(excel_full_path, header=None)

        # csv file name
        csv_file = file.split('.')[0] + '.csv'
        csv_full_path = os.path.join(main_path, csv_file)
        print("Writing the {}".format(csv_file))
        df.to_csv(csv_full_path, encoding='utf-8', index=False, header=False)
    return True

def write_df_and_open(df, folderName:str, fileName:str):
    # checking folderName not created
    target_dir = os.path.abspath(folderName)    # Get absolute path for reliability
    # Create directory with existence check
    try:
        os.makedirs(target_dir, exist_ok=True)
        status = f"Directory created: {target_dir}" if not os.path.exists(target_dir) else f"Directory already exists: {target_dir}"
    except Exception as e:
        status = f"Error creating directory: {str(e)}"

    # join into full path
    fullPath = os.path.join(folderName, fileName)

    # Create a Pandas Excel writer using XlsxWriter as the engine.
    writer = pd.ExcelWriter(fullPath, engine='xlsxwriter')

    df.to_excel(writer, sheet_name='Sheet1')
    # read the worksheet
    worksheet = writer.sheets['Sheet1']
    worksheet.freeze_panes(1, 0)  # freeze first-row
    worksheet.autofit()  # auto fit column width
    worksheet.autofilter(0, 0, df.shape[0], df.shape[1])
    writer.close()

    # open the excel
    os.startfile(os.path.abspath(fullPath))
    print(f"Excel output into {fullPath}")