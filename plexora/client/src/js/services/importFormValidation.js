

// Example starter JavaScript for disabling form submissions if there are invalid fields
(function () {
    'use strict'

    // Fetch all the forms we want to apply custom Bootstrap validation styles to
    var forms = document.querySelectorAll('.needs-validation')
    // Loop over them and prevent submission
    Array.prototype.slice.call(forms)
        .forEach(function (form) {
            form.addEventListener('submit', function (event) {
                if (!form.checkValidity()) {
                    event.preventDefault()
                    event.stopPropagation()
                } else {
                    onupload();
                }
                form.classList.add('was-validated')
            }, true)
        })
})()

//DATASET NAME AUTO-SUGGESTION FROM IMAGE FILE PATH
let datasetNameManuallyEdited = false;

function markDatasetNameEdited() {
    datasetNameManuallyEdited = true;
}

function deriveDatasetName(path) {
    if (!path) return "";
    const base = path.split(/[\\/]/).pop() || "";
    return base.replace(/\.(ome\.tiff|ome\.tif|ome\.zarr|tiff|tif|svs|zarr|png|qptiff)$/i, "");
}

function suggestDatasetName(caller, targetFieldId) {
    if (datasetNameManuallyEdited) return;
    const nameField = document.getElementById(targetFieldId || "name");
    if (!nameField) return;
    const suggested = deriveDatasetName(caller && caller.value);
    if (suggested) nameField.value = suggested;
}

//SOURCE TYPE SELECTION -- segmented control (csv / mcmicro / anndata)
//replaces the old #import_type checkbox now that there are 3 source types
function selectImportType(type) {
    document.querySelectorAll('.source-type-tab').forEach(function (tab) {
        tab.classList.toggle('active', tab.dataset.type === type);
    });
    const forms = {csv: 'custom_form', mcmicro: 'mcmicro_form', anndata: 'anndata_form'};
    Object.keys(forms).forEach(function (key) {
        d3.select('#' + forms[key]).style('display', key === type ? 'block' : 'none');
    });
}

document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.source-type-tab').forEach(function (tab) {
        tab.addEventListener('click', function () {
            selectImportType(tab.dataset.type);
        });
        tab.addEventListener('keydown', function (event) {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                selectImportType(tab.dataset.type);
            }
        });
    });

    // Attaching missing data to an already-registered datasource (see
    // page_routes.py's upload_page()) -- only the CSV/AnnData tabs support
    // that flow (MCMICRO's tab isn't rendered at all in this mode), so land
    // on CSV by default rather than whichever tab happened to be marked
    // active in the template.
    if (window.__attachTo) {
        selectImportType('csv');
    }

    // Wire every "Browse..." button (see browsePicker.js) to fill its
    // paired text field via the native OS file/folder dialog.
    document.querySelectorAll('[data-browse-target]').forEach(function (button) {
        const input = document.getElementById(button.dataset.browseTarget);
        attachBrowseButton(button, input, {
            mode: button.dataset.browseMode || 'file',
            filter: button.dataset.browseFilter || 'any',
        });
    });
});

//check an optional file path -- clears validity entirely when left blank
//instead of flagging an empty optional field as invalid
async function checkOptionalFileExistence(caller) {
    const inputField = d3.select('#' + caller.id);
    if (!inputField.property('value')) {
        inputField.attr('class', 'form-control');
        inputField.node().setCustomValidity('');
        return true;
    }
    return checkFileExistence(caller);
}

//check if path and channel file exist in the specified MCMICRO output foder
async function checkMCOutputFolder(caller) {
    let path_res = await checkPathExistence(caller);
    if (path_res == true) {
        let channel_res = await checkChannelExistence(caller)
        if (channel_res == false) {
            d3.select("#" + 'mcmicro_path_validation_text').html('No image channel file found under this path.')
        }
    } else {
        d3.select("#" + 'mcmicro_path_validation_text').html('Please provide a valid path.')
    }
}

//check the existence of a CSV file (MCMICRO specific)
async function checkCSVFileExistence(caller) {
    const self = this;

    //get folder path from the input text field
    let maskSelectionField = d3.select('#' + caller.id);
    let mask = maskSelectionField.property("value");

    //get selected mask type from the selection field
    let pathInputField = d3.select('#' + 'mcmicro_output_folder');
    let path = pathInputField.property("value");

    try {
        //check if corresponsindg csv file exists
        let response = await fetch(plexoraUrl('check_mc_csv_file_existence'), {
            method: 'POST',
            headers: {
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(
                {
                    path: path,
                    mask: mask
                }
            )
        });
        let response_data = await response.json();
        if (response_data == true) {
            maskSelectionField.attr("class", "form-control is-valid");
            maskSelectionField.node().setCustomValidity('');
        } else {
            d3.select("#" + 'mcmicro_mask_validation_text').html('No corresponding csv file found.')
            maskSelectionField.attr("class", "form-control is-invalid");
            maskSelectionField.node().setCustomValidity('Invalid');
        }
        return response_data;
    } catch (e) {
        console.log("Error While Checking for CSV File Existence", e);
    }
}

//check the existence of the channel file (MCMICRO specific)
async function checkChannelExistence(caller) {
    const self = this;

    //get folder path from the input text field
    let pathInputField = d3.select('#' + caller.id);
    let path = pathInputField.property("value");

    let imageSelectionField = d3.select('#' + caller.id);
    let image = imageSelectionField.property("value");

    try {
        //check if corresponsindg csv file exists
        let response = await fetch(plexoraUrl('check_mc_channel_file_existence'), {
            method: 'POST',
            headers: {
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(
                {
                    path: path,
                    image: image
                }
            )
        });
        let response_data = await response.json();
        if (response_data == true) {
            pathInputField.attr("class", "form-control is-valid");
            pathInputField.node().setCustomValidity('');
        } else {
            // d3.select("#" + 'mcmicro_path_validation_text').html('No image channel file found under this path.')
            pathInputField.attr("class", "form-control is-invalid");
            pathInputField.node().setCustomValidity('No image channel file found under this path.');
        }
        // pathInputField.node().reportValidity();
        return response_data;
    } catch (e) {
        console.log("Error While Checking for Image Channel File Existence", e);
    }
}


//check if path exists (mcmicro naming specific)
async function checkFileExistence(caller) {
    const self = this;
    let inputField = d3.select('#' + caller.id);
    //get segmentation folder path from the input text field
    let path = inputField.property("value");

    try {
        //get available segmentation masks in mcmicro directory from server
        let response = await fetch(plexoraUrl('check_file_existence'), {
            method: 'POST',
            headers: {
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(
                {
                    path: path,
                }
            )
        });
        let response_data = await response.json();
        if (response_data == true) {
            inputField.attr("class", "form-control is-valid");
            inputField.node().setCustomValidity('');
        } else {
            inputField.attr("class", "form-control is-invalid");
            inputField.node().setCustomValidity('Invalid');
        }
        return response_data;
    } catch (e) {
        console.log("Error Getting Segmentation File List", e);
    }
}


//check if dataset already exists
//check if path exists (mcmicro naming specific)
async function checkDatasetExistence(caller) {
    const self = this;
    let inputField = d3.select('#' + caller.id);
    //get segmentation folder path from the input text field
    let datasetName = inputField.property("value");

    try {
        //get available segmentation masks in mcmicro directory from server
        let response = await fetch(plexoraUrl('dataset_existence'), {
            method: 'POST',
            headers: {
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(
                {
                    dataset_name: datasetName,
                }
            )
        });
        let response_data = await response.json();
        if (response_data == false) {
            inputField.attr("class", "form-control is-valid");
            inputField.node().setCustomValidity('');
        } else {
            inputField.attr("class", "form-control is-invalid");
            inputField.node().setCustomValidity('Dataset name already exists. Choose a different name.');
        }
        // inputField.node().reportValidity();
        return response_data;
    } catch (e) {
        console.log("Error Getting Segmentation File List", e);
    }
}

//check if path exists (mcmicro naming specific)
async function checkPathExistence(caller) {
    const self = this;
    let inputField = d3.select('#' + caller.id);
    //get segmentation folder path from the input text field
    let path = inputField.property("value");

    try {
        //get available segmentation masks in mcmicro directory from server
        let response = await fetch(plexoraUrl('check_path_existence'), {
            method: 'POST',
            headers: {
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(
                {
                    path: path,
                }
            )
        });
        let response_data = await response.json();
        if (response_data == true) {
            inputField.attr("class", "form-control is-valid");
            inputField.node().setCustomValidity('');
        } else {
            inputField.attr("class", "form-control is-invalid");
            inputField.node().setCustomValidity('Path does not exist.');
        }
        // inputField.node().reportValidity();
        return response_data;
    } catch (e) {
        console.log("Error Getting Segmentation File List", e);
    }
}

//get a list of available files in a folder (mcmicro naming specific)
async function fillCSVFileList() {
    const self = this;

    //get segmentation folder path from the input text field
    let path = d3.select('#mcmicro_output_folder').property("value");

    //remove old selection options as soon as path changes


    try {
        //get available segmentation masks in mcmicro directory from server
        let response = await fetch(plexoraUrl('get_mc_csv_file_list'), {
            method: 'POST',
            headers: {
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(
                {
                    path: path,
                }
            )
        });
        let response_data = await response.json();
        var select_field = document.getElementById("mcmicro_masks");
        select_field.innerHTML = "";
        //fill select form field with new options
        response_data.forEach(function (option_value) {
            var option = document.createElement("option");
            option.text = option_value;
            option.value = option_value;
            select_field.add(option);
        })

        //return the filled field
        return response_data;
    } catch (e) {
        console.log("Error Getting Segmentation File List", e);
    }
}

//get a list of available files in a folder (mcmicro naming specific)
async function fillImgFileList() {
    const self = this;

    //get segmentation folder path from the input text field
    let path = d3.select('#mcmicro_output_folder').property("value");




    try {
        //get available segmentation masks in mcmicro directory from server
        let response = await fetch(plexoraUrl('get_mc_segmentation_file_list'), {
            method: 'POST',
            headers: {
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(
                {
                    path: path,
                }
            )
        });
        let response_data = await response.json();
        //remove old selection options as soon as path changes
        var select_field = document.getElementById("mcmicro_images");
        select_field.innerHTML = "";
        //fill select form field with new options
        response_data.forEach(function (option_value) {
            var option = document.createElement("option");
            option.text = option_value;
            option.value = option_value;
            select_field.add(option);
        })

        //return the filled field
        return response_data;
    } catch (e) {
        console.log("Error Getting Channel File List", e);
    }
}

//get a list of available files in a folder (mcmicro naming specific)
async function fillSegFileList() {
    const self = this;

    //get segmentation folder path from the input text field
    let path = d3.select('#mcmicro_output_folder').property("value");

    //remove old selection options as soon as path changes


    try {
        //get available segmentation masks in mcmicro directory from server
        let response = await fetch(plexoraUrl('get_mc_segmentation_file_list'), {
            method: 'POST',
            headers: {
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(
                {
                    path: path,
                }
            )
        });
        let response_data = await response.json();
        var select_field = document.getElementById("mcmicro_seg");
        select_field.innerHTML = "";
        //fill select form field with new options
        response_data.forEach(function (option_value) {
            var option = document.createElement("option");
            option.text = option_value;
            option.value = option_value;
            select_field.add(option);
        })

        //return the filled field
        return response_data;
    } catch (e) {
        console.log("Error Getting Segmentation File List", e);
    }
}


//Form submission is intentionally left to the browser's native POST
//navigation (no AJAX interception here) -- the response is a full rendered
//HTML page (channel_match.html / datasource_config.html), and both that
//page and this one load the same base.html script stack (dataLayer.js,
//viewerSidebar.js, etc.), which declare top-level `class`/`let` bindings.
//An AJAX submit + document.write() swap keeps the same JS realm, so those
//declarations collide with the ones already loaded on this page and throw
//"Identifier has already been declared" -- this affected the original
//jquery-form ajaxForm() success handler too (it also called
//document.write()), it just never surfaced because jquery-form@4.3.0's
//ajaxSubmit() calls the removed $.trim() and throws under jQuery 4.x before
//ever reaching a successful response. A real navigation gets a fresh JS
//realm and sidesteps the problem entirely.
//
//uploadPercentage is read by the SSE-driven onupload() below; there's no
//real file upload in these forms (just server-side paths under multipart
//encoding), so it never leaves its initial 0.
let uploadPercentage = 0;

function displayPercentage(totalPercentage, currentTask) {
    if (totalPercentage == 0) {
        $('.progress-bar-label').css('display', 'none');
    } else {
        $('.progress-bar-label').css('display', 'block');
    }
    $('.progress-bar').css('width', totalPercentage + '%').attr('aria-valuenow', totalPercentage);
    $("#progress-bar-percentage").text(totalPercentage + '%');
    $("#progress-bar-current-task").text(currentTask);
}

let consecutiveErrors = 0;
// $('#upload_button').on('click', onupload());
// $('#upload_button_mcmicro').on('click', onupload());

function onupload() {
    uploadPercentage = 0;
    // Hide whatever header exists
    displayHeader('', false, true);
    var source = new EventSource(plexoraUrl("progress"));
    source.onmessage = function (event) {
        let data = JSON.parse(event.data);
        consecutiveErrors = 0;

        if (data.percentage < 0) {
            console.log("Error, Terminating");
            displayPercentage(0, '');
            if (data.currentTask) {
                displayHeader(data.currentTask, true)
            }
            source.close();
            return;
        }
        let combinedPercentage = (data.percentage + (uploadPercentage || 0)) / 2;
        console.log("Parsed Data:", data, "combinedPercentage", combinedPercentage, "UL P", uploadPercentage);
        displayPercentage(combinedPercentage, data.currentTask);
        if (combinedPercentage >= 100) {
            displayHeader("Upload and Conversion Complete", false);
            source.close();
        }
    }
    source.onerror = function (event) {
        consecutiveErrors += 1;
        if (consecutiveErrors > 10) {
            console.log("Error, Terminating");
            displayPercentage(0, '');
            displayHeader("Error", true);
            source.close();
        }
    }
}

function displayHeader(text, isError, hide = false) {
    if (hide) {
        $('#upload-message').empty()
    } else {
        if (isError) {
            $('#upload-message').empty()
            $('#upload-message').append("<span class='error'>" + text + "</span>");
        } else {
            $('#upload-message').empty()
            $('#upload-message').append("<span class='success'>" + text + "</span>");
        }
    }
}
