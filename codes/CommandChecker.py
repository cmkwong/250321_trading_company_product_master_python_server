import os
import sys

from functools import wraps
import pandas as pd
from codes import config
from codes.utils import paramModel, timeModel
from codes.utils.paramModel import command_check, params_check
from codes.controllers.SystemController import SystemController

class CommandChecker:

    def __init__(self):
        self.systemController = SystemController()

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

    @command_check(['pos'])
    def get_position(self):
        self.systemController.capture_xy()
        return self.COMMAND_CHECKED
    #
    # # reading all SSME raw data needed
    # @command_check()
    # def data(self):
    #     self.reportController.base_setUp()
    #     print("report setup OK")
    #     return self.COMMAND_CHECKED
    #
    # # renew item master (Plant)
    # @command_check()
    # def renewMachine(self):
    #     self.reportController.nodeJsServerController.renewMachineData('master')
    #
    # # reading csv and upload into sql server
    # @command_check()
    # @params_check({
    #     'year': ['2022', str],
    #     'month': ['12', str],
    #     'onlyPlants': [[], list]
    # })
    # def sql(self, **params):
    #     self.reportController.processMonth2Server(**params)
    #     return self.COMMAND_CHECKED
    #
    # # reading csv and upload into sql server (combined specific genset IDs -> plantno)
    # @command_check()
    # @params_check({
    #     'year': ['2022', str],
    #     'month': ['12', str],
    #     'convertPlantnosByTo': [
    #         {'by': ['genset935adaf145e64e0d98775ef13eaadf40',
    #                 'genset0d291bd88f7f4d1d84da0882f4a4d369',
    #                 'gensetfdfeaf5f2e504ecda779266f95c8d1e9'],
    #          'to': 'YG1090'}, dict],
    #     'onlyPlants': [[], list]
    # })
    # def sqls(self, **params):
    #     self.reportController.processMonth2Server(**params)
    #     return self.COMMAND_CHECKED
    #
    # # check csv data if healthy
    # @command_check()
    # @params_check({
    #     'year': ['2022', str],
    #     'month': ['12', str],
    #     'checkCsv': [True, bool],
    #     'onlyPlants': [[], list]
    # })
    # def checksql(self, **params):
    #     self.reportController.processMonth2Server(**params)
    #     return self.COMMAND_CHECKED
    #
    # # export Excel data from Web-supervisor then import into MySQL
    # @command_check()
    # @params_check({
    #     "year": ['2023', str],
    #     "month": ['11', str],
    #     "filename": ["Graph-2024-03-07-08-47-02.xlsx", str],
    #     "plantno": ["YG510", str],
    #     "ssmename": ["Act power", str],
    #     "ssmeunit": ["kW", str],
    #     "ssmedecimalplaces": ['0', str],
    # })
    # def wsexcel(self, **params):
    #     self.reportController.processExcel2Server(**params)
    #
    # # create table by NodeJS Server
    # @command_check()
    # @params_check({
    #     'tableName': ['ssme202212', str],
    # })
    # def table(self, **params):
    #     self.reportController.nodeJsServerController.createUnitValueTable(**params)
    #     return self.COMMAND_CHECKED
    #
    # # reading sql and generating tex files
    # @command_check()
    # @params_check({
    #     'report_start_str': ['2025-01-01 00:00:00', str],
    #     'report_end_str': ['2025-01-31 23:59:59', str],
    #     'request_plantnos': [[], list],
    #     'interconnect': ['', bool],
    #     'upload': ['', bool],
    #     'monthly_kwh_needed': ['', bool],
    #     'control_hard_code_co2_unit': ['', str]
    # })
    # def tex(self, **params):
    #     # assign param
    #     self.reportController.loopForEachPlant(**params)
    #     self.reportController.tracker.write_records()  # write the record csv
    #     # self.reportController.conn.close() # close the connection to sqlite
    #     print("Loop plant report tex finished.")
    #     return self.COMMAND_CHECKED
    #
    # # reading sql and generating tex files (with ignore period)
    # @command_check()
    # @params_check({
    #     'report_start_str': ['2023-02-01 00:00:00', str],
    #     'report_end_str': ['2023-02-28 23:59:59', str],
    #     'request_plantnos': [[], list],
    #     'interconnect': ['', bool],
    #     'upload': ['', bool],
    #     'monthly_kwh_needed': ['', bool],
    #     'ignorePeriod': [{'from': '2023-03-05 00:00:00', 'to': '2023-03-13 16:00:00'}, dict]
    # })
    # def texi(self, **params):
    #     # assign param
    #     self.reportController.loopForEachPlant(**params)
    #     self.reportController.tracker.write_records()  # write the record csv
    #     print("Loop plant report tex finished.")
    #     return self.COMMAND_CHECKED
    #
    # # write the pdf
    # @command_check()
    # @params_check({
    #     'year': ['2022', str],
    #     'month': ['1', str]
    # })
    # def write(self, **params):
    #     self.reportController.writePdf(**params)
    #     return self.COMMAND_CHECKED
    #
    # # making tex and writing PDF
    # @command_check()
    # @params_check({
    #     'report_start_str': ['2023-02-01 00:00:00', str],
    #     'report_end_str': ['2023-02-28 23:59:59', str],
    #     'request_plantnos': [[], list],
    #     'interconnect': ['', bool],
    #     'upload': ['', bool],
    #     'monthly_kwh_needed': ['', bool],
    #     'year': ['2022', str],
    #     'month': ['1', str],
    # })
    # def fw(self, **params):
    #     # assign param
    #     self.reportController.loopForEachPlant(**params)
    #     # write the pdf
    #     self.reportController.writePdf(**params)
    #     return self.COMMAND_CHECKED
    #
    # # remove the temp files for SSME
    # @command_check()
    # @params_check({
    #     'year': ['2022', str],
    #     'month': ['4', str],
    #     'request_plantno': [[], list]
    # })
    # def remove(self, **params):
    #     self.reportController.removeRelated(**params)
    #     return self.COMMAND_CHECKED
    #
    # # get the plant of output of electrical current
    # @command_check()
    # @params_check({
    #     'plantnos': [['YG607', 'YG703'], list],
    #     'period_start': ['2022-06-01 00:00:00', str],
    #     'period_end': ['2023-07-31 23:59:59', str]
    # })
    # def plantA(self, **params):
    #     # added the col will be fetched
    #     config.colNameTable['float']['Gen curr L1'] = 'L1_A'
    #     config.colNameTable['float']['Gen curr L2'] = 'L2_A'
    #     config.colNameTable['float']['Gen curr L3'] = 'L3_A'
    #
    #     # looping into each of plant and period
    #     for [start, end] in timeModel.split_month_period(params['period_start'], params['period_end']):
    #         self.reportController.date_setup(start, end)
    #         for plantno in params['plantnos']:
    #             try:
    #                 PlantData = self.reportController.getPlantData(plantno)
    #                 L1_A = PlantData.rawData['L1_A'].resample("1T").mean().interpolate(method="linear",
    #                                                                                    limit_direction="both")
    #                 L2_A = PlantData.rawData['L2_A'].resample("1T").mean().interpolate(method="linear",
    #                                                                                    limit_direction="both")
    #                 L3_A = PlantData.rawData['L3_A'].resample("1T").mean().interpolate(method="linear",
    #                                                                                    limit_direction="both")
    #                 A_df = pd.concat([L1_A, L2_A, L3_A], axis=1)
    #                 filename = f"{plantno}-electrical-current-{start.year}-{start.month}.xlsx"
    #                 A_df.to_excel(os.path.join(config.tempPath, filename))
    #                 print(f"Write {filename} successfully.")
    #
    #             except Exception as e:
    #                 exc_type, exc_obj, tb = sys.exc_info()
    #                 lineno = tb.tb_lineno
    #                 msg = f"{plantno}:{e.__class__.__name__}:{lineno}:{exc_obj}"
    #                 print(msg)
    #     return self.COMMAND_CHECKED
    #
    # # getting running hours for available plants before provided date
    # @command_check()
    # @params_check({
    #     "datetimefrom": ["2024-12-01 00:00:00", str],
    #     "datetimeto": ["2024-12-30 23:59:59", str],
    #     "output_path": ["../docs/runhr.xlsx", str]
    # })
    # def runHrs(self, **params):
    #     # self.reportController.base_setUp()
    #     plantnos = self.reportController.nodeJsServerController.getDistinctPlantno(params['datetimefrom'], params['datetimeto'])
    #     df = self.reportController.nodeJsServerController.getPlantsRunHr(plantnos, params['datetimeto'])
    #     df['last_updated'] = df['last_updated'].str[0:10].str.replace('-', '')
    #     df.to_excel(params['output_path'])
    #     return self.COMMAND_CHECKED
    #
    # # running hours for a plant
    # @command_check()
    # @params_check({
    #     'plantno': ['YG703', str],
    #     'start_str': ['2023-06-01 00:00:00', str],
    #     'end_str': ['2023-07-31 23:59:59', str]
    # })
    # def runHrExcel(self, **params):
    #     try:
    #         self.reportController.date_setup(params['start_str'], params['end_str'])
    #         # get data
    #         PlantData = self.reportController.getPlantData(params['plantno'])
    #         # drop the na data rows
    #         df = PlantData.rawData['run_hours'].dropna()
    #         # output the file
    #         filename = f"{params['plantno']}_{params['start_str'].split(' ')[0]}_{params['end_str'].split(' ')[0]}_runhr.xlsx"
    #         fullPath = os.path.join(config.tempPath, filename)
    #         df.to_excel(fullPath)
    #         print(f"Excel is written - {fullPath}.")
    #     except Exception as e:
    #         exc_type, exc_obj, tb = sys.exc_info()
    #         lineno = tb.tb_lineno
    #         msg = f"{params['plantno']}:{e.__class__.__name__}:{lineno}:{exc_obj}"
    #         print(msg)
    #     return self.COMMAND_CHECKED
    #
    # # ---------------------------------- AC statement split ----------------------------------
    # @command_check()
    # @params_check({
    #     'folderName': ['20240815', str],
    #     'domain': [0, int, "0=Company, 1=Salesman"],
    #     'path': ["Z:/AC/Operator/Operation/AR/Customers Statements/", str]
    # })
    # def statement(self, **params):
    #     # run statement query
    #     self.statementSplitController.run(**params)
    #     return self.COMMAND_CHECKED
    # else:
    #     return self.command_not_checked
