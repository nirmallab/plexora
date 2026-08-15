# CRUD for Datasources

# import sys
# sys.path.append('/c/Users/Sophie/plexora/')
from plexora import app, get_config, get_config_names, config_json_path, data_path, cwd_path
from plexora.server.utils import mostFrequentLongestSubstring, pre_normalization
from plexora.server.models import data_model
from plexora.server.routes.page_routes import template_data

from flask import render_template, request, Response, jsonify, redirect
from pathlib import Path
from pathlib import PurePath

import werkzeug.datastructures as wz
import numpy as np
import polars as pl
import shutil
import csv
import json
import orjson
import os
import datetime
from os import walk
import io


total_tasks = 100
completed_task = 0
current_task = ''


@app.route('/edit_config', methods=['GET'])
def edit_config_with_request_object():
    config_name = request.args.get("config")
    return edit_config_with_config_name(config_name)


@app.route('/edit_config/<string:config_name>')
def edit_config_with_request_name(config_name):
    return edit_config_with_config_name(config_name)


@app.route('/delete/<string:config_name>')
def delete_with_datasource_name(config_name):
    global config_json_path

    path = str(data_path / config_name)
    if Path(path).exists():
        shutil.rmtree(path)
    with open(config_json_path, "r+") as configJson:
        config_data = json.load(configJson)
        del config_data[config_name]
        configJson.seek(0)  # <--- should reset file position to the beginning.
        json.dump(config_data, configJson, indent=4)
        configJson.truncate()
    base_url = app.config.get('PLEXORA_BASE_URL', '')
    return redirect(f"{base_url}/open_project")


def edit_config_with_config_name(config_name):
    data = {}
    global config_json_path
    with open(config_json_path, "r+") as configJson:
        config_csv = json.load(configJson)
        config_data = config_csv[config_name]
        data['datasetName'] = config_name
        # test_data['channelFileNames'] = ['channel_01', 'channel_02']
        data['csvName'] = config_data['featureData'][0]['src'].split("/")[-1]
        if 'celltypeData' in config_data['featureData'][0]:
            data['celltypeData'] = config_data['featureData'][0]['celltypeData']

        if 'shapes' in config_data:
            data['shapes'] = config_data['shapes']

        if 'activeChannel' in config_data:
            data['activeChannel'] = config_data['activeChannel']

        if 'normalization' in config_data['featureData'][0]:
            data['normalization'] = config_data['featureData'][0]['normalization']

        if 'isTransformed' in config_data['featureData'][0]:
            data['isTransformed'] = config_data['featureData'][0]['isTransformed']

        if 'clusterData' in config_data:
            data['normCsvName'] = config_data['clusterData']

        if 'maxLevel' in config_data:
            data['maxLevel'] = config_data['maxLevel']
        if 'height' in config_data:
            data['height'] = config_data['height']

        if 'width' in config_data:
            data['width'] = config_data['width']

        if 'segmentation' in config_data:
            data['segmentation'] = config_data['segmentation']

        if 'channelFile' in config_data:
            data['channelFile'] = config_data['channelFile']

        if 'num_channels' in config_data:
            data['num_channels'] = config_data['num_channels']

        if 'tileHeight' in config_data:
            data['tileHeight'] = config_data['tileHeight']

        if 'tileWidth' in config_data:
            data['tileWidth'] = config_data['tileWidth']

        csvHeaders = []
        channelFileNames = []
        if 'idField' in config_data['featureData'][0]:
            data['idField'] = True
            elem = {}
            elem['fullName'] = config_data['featureData'][0]['idField']
            elem['displayName'] = config_data['featureData'][0]['idField']
            csvHeaders.append(elem)
            channelFileNames = ['ID']
        else:
            data['idField'] = False;
        # add x cord
        elem = {}
        elem['fullName'] = config_data['featureData'][0]['xCoordinate']
        elem['displayName'] = config_data['featureData'][0]['xCoordinate']
        csvHeaders.append(elem)
        # add y cord
        elem = {}
        elem['fullName'] = config_data['featureData'][0]['yCoordinate']
        elem['displayName'] = config_data['featureData'][0]['yCoordinate']
        csvHeaders.append(elem)
        # add cell type
        if 'celltypeData' in config_data['featureData'][0]:
            elem = {}
            elem['fullName'] = config_data['featureData'][0]['celltype']
            elem['displayName'] = config_data['featureData'][0]['celltype']
            csvHeaders.append(elem)

        # Start with the required channels
        if 'celltypeData' in config_data['featureData'][0]:
            channelFileNames.extend(['Area', 'X Position', 'Y Position', 'Cell Type'])
        else:
            channelFileNames.extend(['Area', 'X Position', 'Y Position'])

        for i in range(len(config_data['imageData'])):
            elem = config_data['imageData'][i]
            channelName = elem['src'].split("/")[-2]
            header = {}
            header['fullName'] = elem['fullname']
            header['displayName'] = elem['name']
            # Special handling for label channel
            if i == 0:
                data['labelName'] = channelName
                if data['idField']:
                    csvHeaders.insert(1, header)
                else:
                    csvHeaders.insert(0, header)
            else:
                channelFileNames.append(channelName)
                csvHeaders.append(header)

        data['csvHeader'] = csvHeaders
        header_full_names = [elem['displayName'] for elem in csvHeaders]
        data['substring'] = mostFrequentLongestSubstring.find_substring(header_full_names)
        data['channelFileNames'] = channelFileNames
        data['datasources'] = [key for key in config_csv.keys()]
        data['is_docker'] = app.config.get('IS_DOCKER', False)
        data['base_url'] = app.config.get('PLEXORA_BASE_URL', '')
        return render_template('channel_match.html', data=template_data(**data))


@app.route('/upload', methods=['GET', 'POST'])
def upload_file_page():
    global total_tasks
    global completed_task
    global current_task
    total_tasks = 1
    completed_task = 0
    current_task = "Uploading"
    datasetName = None
    csvName = ''
    celltypeName = ''
    channelFileNames = ['ID', 'Area', 'X Position', 'Y Position']
    labelName = ''
    csvHeader = None
    if request.method == 'POST':
        try:
            if request.form['action'] == 'Upload':
                # Set when this upload is attaching missing feature/segmentation data to an
                # already-registered (e.g. quick-view) datasource rather than creating a new
                # one -- see page_routes.py's upload_page() and tool_routes.py's open_tool().
                attach_to = request.form.get('attach_to') or ''
                return_tool = request.form.get('return_tool') or ''
                # if we have a fully user specified data upload
                if request.form.get('mcmicro_name') is None:
                    if attach_to and request.form.get('name') != attach_to:
                        raise ValueError("Attach-mode uploads must keep the original dataset name.")
                    #dataset name
                    datasetName = request.form['name']

                    #label file
                    labelFile = request.form.get('label_file')
                    labelFile = trim_filepath_quotes(labelFile)
                    labelFile = Path(labelFile)
                    labelName = os.path.splitext(labelFile.name)[0]

                    #csv file
                    csvPath = request.form.get('csv_file');
                    csvPath = trim_filepath_quotes(csvPath)
                    csvPath = Path(csvPath)
                    pathsSplit = PurePath(csvPath).parts
                    csvName = pathsSplit[len(pathsSplit) - 1]

                    #channel file
                    channelFile = request.form.get('channel_file')
                    channelFile = trim_filepath_quotes(channelFile)
                    channelFile = Path(channelFile)

                # if a mcmicro output structure is used
                else:
                    if attach_to:
                        raise ValueError("MCMICRO import is not supported when attaching data to an existing datasource.")
                    directory = request.form['mcmicro_output_folder']
                    pathsSplit = PurePath(directory).parts
                    mcmicroDirName = pathsSplit[len(pathsSplit)-1]

                    #dataset name is optional, if not provided mcmicro name is used
                    datasetName = mcmicroDirName
                    if request.form.get('mcmicro_name') != '':
                        datasetName = request.form['mcmicro_name']

                    #label file
                    labelFile = request.form.get('segs')
                    labelFile = labelFile.replace('"', '') # remove " characters
                    labelFile = Path(labelFile)
                    labelName = os.path.splitext(labelFile.name)[0]

                    #csv file
                    csvPath = request.form.get('masks');
                    csvPath = csvPath.replace('"', '') # remove " characters
                    csvPath = Path(csvPath)
                    pathsSplit = PurePath(csvPath).parts
                    csvName = pathsSplit[len(pathsSplit) - 1]

                    #channel file
                    channelFile = request.form.get('images')
                    channelFile = channelFile.replace('"', '') # remove " characters
                    channelFile = Path(channelFile)

                # Creates file path using name input; should change this so that it just takes directory name?
                file_path = str(PurePath(Path.cwd(), data_path, datasetName))
                if not Path(file_path).exists(): # If no directory for existing name for dataset input will create one
                    Path(file_path).mkdir()
                total_tasks = 2


                # Process CSV File

                #open original csv location
                csvFile = [open(csvPath)]
                # file path to write to on server
                f = open(str(Path(file_path) / csvName), 'w')
                # write to new location on server
                f.write(csvFile[0].read())
                # read field names from new server location
                with open(csvPath, 'r') as infile:
                    reader = csv.DictReader(infile)
                    csvHeader = reader.fieldnames

                # Process Channel File
                current_task = "Converting OME-TIFF Channels (This Will Take a While)"
                if attach_to:
                    # Attaching data to an already-registered image -- reuse its existing
                    # tiled channels instead of re-deriving/re-tiling them, and ignore any
                    # (locked, but not implicitly trusted) client-supplied channel_file.
                    existing_entry = get_config().get(attach_to)
                    if existing_entry is None:
                        raise ValueError(f"'{attach_to}' is no longer a registered datasource.")
                    channel_info = {
                        'height': existing_entry['height'],
                        'width': existing_entry['width'],
                        'maxLevel': existing_entry['maxLevel'],
                        'num_channels': existing_entry['num_channels'],
                        'tileHeight': existing_entry['tileHeight'],
                        'tileWidth': existing_entry['tileWidth'],
                        'channel_names': [
                            item['src'].rstrip('/').split('/')[-1] for item in existing_entry['imageData']
                        ],
                    }
                    channelFile = Path(existing_entry['channelFile'])
                else:
                    channel_info = data_model.convertOmeTiff(channelFile, isLabelImg=False)
                channelFileNames.extend(channel_info['channel_names'])
                completed_task += 1

                #Process Segmentation File -- pyramid/outline generation can take real
                #time on a large mask, so it runs in the background instead of blocking
                #this request; the viewer opens as soon as the (cheap, metadata-only)
                #main image conversion above and the config write below are done, with
                #the segmentation layer appearing once the background job finishes (see
                #data_model.start_segmentation_job / GET /get_segmentation_status).
                current_task = "Converting Segmentation Mask (running in background)"
                data_model.start_segmentation_job(datasetName, labelFile, channelFile, file_path)
                completed_task += 1
                current_task = total_tasks
                current_task = 'Complete'
                config_data = {}
                full_csv_header = []

                # now iterate fields for the config overview
                for header in csvHeader:
                    elem = {}
                    elem['fullName'] = header
                    full_csv_header.append(elem)

                config_data['csvHeader'] = full_csv_header
                header_full_names = [elem['fullName'] for elem in full_csv_header]
                config_data['substring'] = mostFrequentLongestSubstring.find_substring(header_full_names)
                config_data['datasetName'] = datasetName

                config_data['maxLevel'] = channel_info['maxLevel']
                config_data['height'] = channel_info['height']
                config_data['width'] = channel_info['width']
                config_data['segmentation'] = None
                config_data['segmentation_status'] = 'pending'

                config_data['num_channels'] = channel_info['num_channels']
                config_data['tileHeight'] = channel_info['tileHeight']
                config_data['tileWidth'] = channel_info['tileWidth']

                config_data['datasetName'] = datasetName
                config_data['channelFileNames'] = channelFileNames
                config_data['csvName'] = csvName

                config_data['channelFile'] = str(channelFile)
                config_data['new'] = True
                config_data['labelName'] = labelName
                config_data['datasources'] = get_config_names()
                config_data['datasources'].append(datasetName)

                datasource = pl.read_csv(csvPath)
                listNotMarkers = ['CellID', 'X_centroid', 'Y_centroid', 'Area', 'MajorAxisLength', 'MinorAxisLength', 'Eccentricity', 'Solidity', 'Extent', 'Orientation', 'column_centroid', 'row_centroid', 'phenotype']
                listImageData = [name for name in header_full_names if name not in listNotMarkers]
                datasourceImageData = datasource.select(listImageData)
                col_means = datasourceImageData.mean().row(0)
                if float(np.mean(col_means)) < 15:
                    config_data["isTransformed"] = True
                else:
                    config_data["isTransformed"] = False

                config_data['is_docker'] = app.config.get('IS_DOCKER', False)
                config_data['base_url'] = app.config.get('PLEXORA_BASE_URL', '')
                config_data['attach_to'] = attach_to
                config_data['return_tool'] = return_tool
                return render_template('channel_match.html', data=template_data(**config_data))
        except Exception as e:
            completed_task = -1
            current_task = str(e)
            return render_template('index.html', data=template_data())
            # Now Edit Config.Json With my my Data
    print("Finished Updating Config.json")
    return render_template('index.html', data=template_data())


@app.route('/progress')
def progress():
    def generate():
        global total_tasks
        global completed_task
        global current_task
        data = {}
        # Error Handling
        if current_task == -1:
            data['percentage'] = -1
            data['currentTask'] = current_task
        else:
            if total_tasks == 0:
                total_tasks = 100
            percentage = int((completed_task / total_tasks) * 100)
            data['percentage'] = percentage
            data['currentTask'] = current_task
        print("Percentage:", percentage, completed_task, total_tasks, current_task)
        yield "data:" + json.dumps(data) + "\n\n"

    return Response(generate(), mimetype='text/event-stream')


@app.route('/save_config', methods=['POST'])
def save_config():
    global config_json_path
    try:
        originalData = request.json['originalData']
        datasetName = originalData['datasetName']
        csvName = originalData['csvName']
        if 'celltypeData' in originalData:
            celltypeName = originalData['celltypeData']
        idList = request.json['idField']
        headerList = request.json['headerList']

        isTransformed = originalData['isTransformed']
        transformData = request.json['transformData']
        if not isTransformed and transformData:
            print("Transforming Data")
            skip_columns = []
            if idList[2]['value'] != 'on':
                skip_columns.append(idList[0]['value'])
            for i in range(int(len(headerList) / 3)):
                column_name = headerList[i * 3]['value']
                normalize_column = headerList[i * 3 + 2]['value']
                if normalize_column != 'on':
                    skip_columns.append(column_name)
            file_path = str(Path(cwd_path, data_path, datasetName))
            csvPath = str(Path(file_path) / csvName)
            # pre_normalization.preNormalize(csvPath, normPath, skip_columns=skip_columns)
            data_model.logTransform(csvPath, skip_columns=skip_columns)
            print("Finished Transforming Data")
        # elif 'normalizeCsvName' in request.json:
        #     normCsvName = request.json['normalizeCsvName']
        # else:
        #     normCsvName = None
        if 'normalizeCsvName' in request.json:
            normCsvName = request.json['normalizeCsvName']
        else:
            normCsvName = None

        headerList = [x for x in zip(headerList[1::3], headerList[0::3])]
        channelList = originalData['channelFileNames']
        with open(config_json_path, "r+") as configJson:
            configData = json.load(configJson)
            existing_created_at = configData.get(datasetName, {}).get('createdAt')
            configData[datasetName] = {}
            # save_config is the CSV-attach / full-upload commit path -- a real
            # feature CSV is always being written here (see featureData below),
            # so this is the first-class "has real feature data" state.
            configData[datasetName]['has_feature_data'] = True
            configData[datasetName]['createdAt'] = existing_created_at or datetime.datetime.now().isoformat()
            configData[datasetName]['shapes'] = ''
            if normCsvName:
                configData[datasetName]['clusterData'] = normCsvName
            configData[datasetName]['activeChannel'] = ''
            configData[datasetName]['featureData'] = [{}]
            configData[datasetName]['featureData'][0]['normalization'] = 'none'
            if 'celltypeData' in originalData:
                configData[datasetName]['featureData'][0]['celltypeData'] = str(data_path / datasetName / celltypeName)
                configData[datasetName]['featureData'][0]['celltype'] = headerList[3][1]['value']
            configData[datasetName]['featureData'][0]['xCoordinate'] = headerList[1][1]['value']
            configData[datasetName]['featureData'][0]['yCoordinate'] = headerList[2][1]['value']

            # If optional id field
            if 'idField' in request.json:
                channelList.pop(0)
                configData[datasetName]['featureData'][0]['idField'] = request.json['idField'][1]['value']

            if 'shapes' in originalData:
                configData[datasetName]['shapes'] = originalData['shapes']

            if 'height' in originalData:
                configData[datasetName]['height'] = originalData['height']

            if 'width' in originalData:
                configData[datasetName]['width'] = originalData['width']

            if 'maxLevel' in originalData:
                configData[datasetName]['maxLevel'] = originalData['maxLevel']

            if 'num_channels' in originalData:
                configData[datasetName]['num_channels'] = originalData['num_channels']

            if 'tileWidth' in originalData:
                configData[datasetName]['tileWidth'] = originalData['tileWidth']

            if 'tileHeight' in originalData:
                configData[datasetName]['tileHeight'] = originalData['tileHeight']

            # Don't trust the client-echoed originalData['segmentation'] --
            # it's a snapshot from /upload time and may be stale relative to
            # the background segmentation job (data_model.start_segmentation_job)
            # that's been running while the user filled out this form. Look up
            # the live status server-side instead.
            job_status = data_model.get_segmentation_job_status(datasetName)
            if job_status['status'] == 'ready':
                configData[datasetName]['segmentation'] = job_status['segmentation']
                configData[datasetName]['segmentation_status'] = 'ready'
            elif job_status['status'] == 'error':
                configData[datasetName]['segmentation'] = None
                configData[datasetName]['segmentation_status'] = 'error'
            else:
                configData[datasetName]['segmentation'] = None
                configData[datasetName]['segmentation_status'] = 'pending'

            if 'channelFile' in originalData:
                configData[datasetName]['channelFile'] = originalData['channelFile']

            if 'activeChannel' in originalData:
                configData[datasetName]['activeChannel'] = originalData['activeChannel']

            if 'normalization' in originalData:
                configData[datasetName]['featureData'][0]['normalization'] = originalData['normalization']

            if isTransformed or transformData:
                configData[datasetName]['featureData'][0]['isTransformed'] = True
            else:
                configData[datasetName]['featureData'][0]['isTransformed'] = False

            configData[datasetName]['featureData'][0][
                'src'] = str(data_path / datasetName / csvName)
            # Adding the Label Channel as the First Label
            configData[datasetName]['imageData'] = [{}]
            configData[datasetName]['imageData'][0]['name'] = headerList[0][1]['value']
            configData[datasetName]['imageData'][0]['fullname'] = 'Area'
            if 'labelName' in originalData and originalData['labelName'] != '':
                configData[datasetName]['imageData'][0]['src'] = "/generated/data/" + datasetName + "/" + originalData[
                    'labelName'] + "/"
            else:
                configData[datasetName]['imageData'][0]['src'] = ''

            if 'celltypeData' in originalData:
                channelList = channelList[4:]
            else:
                channelList = channelList[3:]

            if 'celltypeData' in originalData:
                channelStart = 4
            else:
                channelStart = 3
            for i in range(len(channelList)):
                channel = channelList[i]
                channelData = {}
                channelData['src'] = "/generated/data/" + datasetName + "/" + channel + "/"
                channelData['name'] = headerList[i + channelStart][0]['value']
                channelData['fullname'] = headerList[i + channelStart][1]['value']
                configData[datasetName]['imageData'].append(channelData)
            configJson.seek(0)  # <--- should reset file position to the beginning.
            json.dump(configData, configJson, indent=4)
            configJson.truncate()
            data_model.load_datasource(datasetName, reload=True)
            resp = jsonify(success=True)
            return resp

    except Exception as e:
        resp = jsonify(success=False)
        return resp

@app.route('/get_mc_segmentation_file_list', methods=['POST'])
def list_tif_files_in_dir():
    # return all seg files found in the seg subfolder (mc micro specific)
    files = []
    files.append('')

    #path and type information from upload
    post_data = json.loads(request.data)
    if 'path' in post_data:
        # path = Path(post_data['path'], "segmentation");
        path = Path(post_data['path'])

        #for segmentation, mcmicro specifics
        # mask_types = ["cell", "cellRing", "cyto", "cytoRing", "nuclei", "nucleiRing"]

        if path.is_dir():
            for (dirpath, dirnames, filenames) in walk(path):
                for (file) in filenames:
                    file_split = file.split('.')
                    # if file[0] in mask_types:
                    #     files.append(file[0])
                    if (file_split[-1] == 'tif' and file_split[-2] == 'ome') or (file_split[-1] == 'tiff' and file_split[-2] == 'ome'):
                        file_path = os.path.join(dirpath, file)
                        files.append(file_path)
            print(files)
    else:
        print('error in segmentation path');
    return serialize_and_submit_json(files)

@app.route('/get_mc_csv_file_list', methods=['POST'])
def list_csv_files_in_dir():
    # return all seg files found in the seg subfolder (mc micro specific)
    files = []
    files.append('')

    #path and type information from upload
    post_data = json.loads(request.data)
    if 'path' in post_data:
        # path = Path(post_data['path'], "segmentation");
        path = Path(post_data['path'])

        #for segmentation, mcmicro specifics
        # mask_types = ["cell", "cellRing", "cyto", "cytoRing", "nuclei", "nucleiRing"]

        if path.is_dir():
            for (dirpath, dirnames, filenames) in walk(path):
                for (file) in filenames:
                    file_split = file.split('.')
                    # if file[0] in mask_types:
                    #     files.append(file[0])
                    if file_split[-1] == 'csv':
                        file_path = os.path.join(dirpath, file)
                        files.append(file_path)
            print(files)
    else:
        print('error in csv path');
    return serialize_and_submit_json(files)

@app.route('/check_mc_csv_file_existence', methods=['POST'])
def check_mc_csv_file_existence():
    # path and type information from upload
    post_data = json.loads(request.data)
    if 'path' in post_data:
        if 'mask' in post_data:
            #get path and last bit which defines the dirname
            mask = post_data['mask']
            directory = Path(post_data['path'])
            pathsSplit = PurePath(directory).parts
            mcmicroDirName = pathsSplit[len(pathsSplit) - 1]
        
            path = Path(post_data['mask'])
            if path.suffix.lower() == '.csv':
                return serialize_and_submit_json(True)

    return serialize_and_submit_json(False)

@app.route('/check_mc_channel_file_existence', methods=['POST'])
def check_mc_channel_file_existence():
    # path and type information from upload
    post_data = json.loads(request.data)
    if 'path' in post_data:

        if 'image' in post_data:
            path = Path(post_data['image'])
            if path.suffix.lower() == '.tif' or '.tiff':
                return serialize_and_submit_json(True)

    return serialize_and_submit_json(False)

def trim_filepath_quotes(path):
    if path.startswith('"') and path.endswith('"'):
        return path[1:-1]
    if path.startswith("'") and path.endswith("'"):
        return path[1:-1]
    return path

@app.route('/check_file_existence', methods=['POST'])
def check_file_existence():
    # path and type information from upload
    post_data = json.loads(request.data)
    # Handle path that begins and ends
    if 'path' in post_data:
        path = trim_filepath_quotes(post_data['path'])
        if Path(path).is_file():
            return serialize_and_submit_json(True)
        return serialize_and_submit_json(False)

@app.route('/check_path_existence', methods=['POST'])
def check_path_existence():
    # path and type information from upload
    post_data = json.loads(request.data)
    if 'path' in post_data:
        path = trim_filepath_quotes(post_data['path'])
        # if full path exists
        if Path(path).is_dir():
            return serialize_and_submit_json(True)
        # if path does not exist
        return serialize_and_submit_json(False)

@app.route('/dataset_existence', methods=['POST'])
def check_dataset_exists():
    # path and type information from upload
    post_data = json.loads(request.data)
    if 'dataset_name' in post_data:
        dataset_name = Path(post_data['dataset_name'])
        # if path exists as a relative path inside the data folder
        path = Path(Path.cwd(), data_path, dataset_name);
        if not path.is_dir() or dataset_name.name == '':
            return serialize_and_submit_json(False)
    return serialize_and_submit_json(True)

@app.route('/init_datasource', methods=['GET'])
def init_datasource():
    datasource = request.args.get('datasource')
    data_model.init(datasource)
    resp = jsonify(success=True)
    return resp


def serialize_and_submit_json(data):
    response = app.response_class(
        response=orjson.dumps(data, option=orjson.OPT_SERIALIZE_NUMPY),
        mimetype='application/json'
    )
    return response
