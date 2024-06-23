import os, io, logging, json, time, re
from datetime import datetime
from threading import Condition
import threading
import cv2
import base64
from openai import OpenAI #pip install openai

from flask import Flask, render_template, request, jsonify, Response, send_file, abort

from PIL import Image

from picamera2 import Picamera2
from picamera2.encoders import JpegEncoder
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput
from libcamera import Transform, controls


# Int Flask
app = Flask(__name__)

# OpenAI API key (Your own API key here!)
open_ai_api_key = 'TYPE IN YOUR OPENAI API KEY HERE!'

# Place your OpenAI prompt here:
#openai_prompt = "This is a photo. Describe the person's face mimic and gestic. How does the person feel? Describe in three sentences."
openai_prompt = 'Das ist ein Foto. Beschreibe die Mimik und Gestik der Person. Wie fühlt sich die Person gerade? Beschreibe in maximal drei Sätzen.'

# Int Picamera2 and default settings
picam2 = Picamera2()

# Int Picamera2 and default settings
timelapse_running = False
timelapse_thread = None

# Get the directory of the current script
current_dir = os.path.dirname(os.path.abspath(__file__))
# Define the path to the camera-config.json file
camera_config_path = os.path.join(current_dir, 'camera-config.json')
# Pull settings from from config file
with open(camera_config_path, "r") as file:
    camera_config = json.load(file)
# Print config for validation
print(f'\nCamera Config:\n{camera_config}\n')

# Split config for different uses
live_settings = camera_config.get('controls', {})
rotation_settings = camera_config.get('rotation', {})
sensor_mode = camera_config.get('sensor-mode', 1)
capture_settings = camera_config.get('capture-settings', {}) 

# Parse the selected capture resolution for later
selected_resolution = capture_settings["Resolution"]
resolution = capture_settings["available-resolutions"][selected_resolution]
print(f'\nCamera Settings:\n{capture_settings}\n')
print(f'\nCamera Set Resolution:\n{resolution}\n')

# Get the sensor modes and pick from the the camera_config
camera_modes = picam2.sensor_modes
mode = picam2.sensor_modes[sensor_mode]

# Create the video_config 
video_config = picam2.create_video_configuration(main={'size':resolution}, sensor={'output_size': mode['size'], 'bit_depth': mode['bit_depth']})
print(f'\nVideo Config:\n{video_config}\n')

# Pull default settings and filter live_settings for anything picamera2 wont use (because the not all cameras use all settings)
default_settings = picam2.camera_controls
live_settings = {key: value for key, value in live_settings.items() if key in default_settings}

# Define the path to the camera-module-info.json file
camera_module_info_path = os.path.join(current_dir, 'camera-module-info.json')
# Load camera modules data from the JSON file
with open(camera_module_info_path, "r") as file:
    camera_module_info = json.load(file)
camera_properties = picam2.camera_properties
print(f'\nPicamera2 Camera Properties:\n{camera_properties}\n')

# Set the path where the images will be stored
UPLOAD_FOLDER = os.path.join(current_dir, 'static/gallery')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Create the upload folder if it doesn't exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

##################
# Streaming Class
##################

output = None
class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()

def generate():
    global output
    while True:
        with output.condition:
            output.condition.wait()
            frame = output.frame
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')

#################################
# Load Config from file Function
#################################

# Load camera settings from config file
def load_settings(settings_file):
    try:
        with open(settings_file, 'r') as file:
            settings = json.load(file)
            print(settings)
            return settings
    except FileNotFoundError:
        # Return default settings if the file is not found
        logging.error(f"Settings file {settings_file} not found")
        return None
    except Exception as e:
        logging.error(f"Error loading camera settings: {e}")
        return None

#######################################
# Site Routes (routes to actual pages)
#######################################

@app.route("/")
def home():
    return render_template("home.html", page='home')

@app.route("/camera")
def camera():
    return render_template("camera.html", page='camera', title="KIPI-Camera", live_settings=live_settings, rotation_settings=rotation_settings, settings_from_camera=default_settings, capture_settings=capture_settings)

@app.route('/video_feed')
def video_feed():
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/about")
def about():
    return render_template("about.html", page='about')

###################################################
# Setting Routes (routes that manipulate settings)
###################################################

# Route to update settings to the buffer
@app.route('/update_live_settings', methods=['POST'])
def update_settings():
    global live_settings, capture_settings, picam2, video_config, resolution, sensor_mode, mode
    try:
        # Parse JSON data from the request
        data = request.get_json()
        print(data)
        # Update only the keys that are present in the data
        for key in data:
            if key in live_settings:
                print(key)
                if key in ('AfMode', 'AeConstraintMode', 'AeExposureMode', 'AeFlickerMode', 'AeFlickerPeriod', 'AeMeteringMode', 'AfRange', 'AfSpeed', 'AwbMode', 'ExposureTime') :
                    live_settings[key] = int(data[key])
                elif key in ('Brightness', 'Contrast', 'Saturation', 'Sharpness', 'ExposureValue', 'LensPosition'):
                    live_settings[key] = float(data[key])
                elif key in ('AeEnable', 'AwbEnable', 'ScalerCrop'):
                    live_settings[key] = data[key]
                # Update the configuration of the video feed
                configure_camera(live_settings)
                return jsonify(success=True, message="Settings updated successfully", settings=live_settings)
            elif key in capture_settings:
                if key in ('Resolution'):
                    capture_settings['Resolution'] = int(data[key])
                    selected_resolution = int(data[key])
                    resolution = capture_settings["available-resolutions"][selected_resolution]
                    stop_camera_stream()
                    video_config = picam2.create_video_configuration(main={'size':resolution}, sensor={'output_size': mode['size'], 'bit_depth': mode['bit_depth']})
                    start_camera_stream()
                    return jsonify(success=True, message="Settings updated successfully", settings=capture_settings)
                elif key in ('makeRaw'):
                    capture_settings[key] = data[key]
                    return jsonify(success=True, message="Settings updated successfully", settings=capture_settings)
            elif key == ('sensor_mode'):
                sensor_mode = int(data[key])
                mode = picam2.sensor_modes[sensor_mode]
                stop_camera_stream()
                video_config = picam2.create_video_configuration(main={'size':resolution}, sensor={'output_size': mode['size'], 'bit_depth': mode['bit_depth']})
                start_camera_stream()
                save_sensor_mode(sensor_mode)
                return jsonify(success=True, message="Settings updated successfully", settings=sensor_mode)
    except Exception as e:
        return jsonify(success=False, message=str(e))

# Route to update settings that requires a restart of the stream
@app.route('/update_restart_settings', methods=['POST'])
def update_restart_settings():
    global rotation_settings, video_config
    try:
        data = request.get_json()
        stop_camera_stream()
        transform = Transform()
        # Update settings that require a restart
        for key, value in data.items():
            if key in rotation_settings:
                if key in ('hflip', 'vflip'):
                    rotation_settings[key] = data[key]
                    setattr(transform, key, value)
                video_config["transform"] = transform     
        start_camera_stream()
        return jsonify(success=True, message="Restart settings updated successfully", settings=live_settings)
    except Exception as e:
        return jsonify(success=False, message=str(e))

@app.route('/reset_default_live_settings', methods=['GET'])
def reset_default_live_settings():
    global live_settings, rotation_settings
    try:
        # Get the default settings from picam2.camera_controls
        default_settings = picam2.camera_controls

        # Apply only the default values to live_settings
        for key in default_settings:
            if key in live_settings:
                min_value, max_value, default_value = default_settings[key]
                live_settings[key] = default_value if default_value is not None else max_value
        configure_camera(live_settings)

        # Reset rotation settings and restart stream
        for key, value in rotation_settings.items():
            rotation_settings[key] = 0
        restart_configure_camera(rotation_settings)

        return jsonify(data1=live_settings, data2=rotation_settings)
    except Exception as e:
        return jsonify(error=str(e))

# Add a new route to save settings
@app.route('/save_settings', methods=['GET'])
def save_settings():
    global live_settings, rotation_settings, capture_settings, camera_config
    try:
        with open('camera-config.json', 'r') as file:
            camera_config = json.load(file)

        # Update controls in the configuration with live_settings
        for key, value in live_settings.items():
            if key in camera_config['controls']:
                camera_config['controls'][key] = value

        # Update controls in the configuration with rotation settings
        for key, value in rotation_settings.items():
            if key in camera_config['rotation']:
                camera_config['rotation'][key] = value

        # Update controls in the configuration with rotation settings
        for key, value in capture_settings.items():
            if key in camera_config['capture-settings']:
                camera_config['capture-settings'][key] = value
        
        # Save current camera settings to the JSON file
        with open('camera-config.json', 'w') as file:
            json.dump(camera_config, file, indent=4)

        return jsonify(success=True, message="Settings saved successfully")
    except Exception as e:
        logging.error(f"Error in saving data: {e}")
        return jsonify(success=False, message=str(e))
    
def save_sensor_mode(sensor_mode):
    try:
        with open('camera-config.json', 'r') as file:
            camera_config = json.load(file)

        # Update sensor mode
        camera_config['sensor-mode'] = sensor_mode
        
        # Save current camera settings to the JSON file
        with open('camera-config.json', 'w') as file:
            json.dump(camera_config, file, indent=4)

        return jsonify(success=True, message="Settings saved successfully")
    except Exception as e:
        logging.error(f"Error in saving data: {e}")
        return jsonify(success=False, message=str(e))


###################################################################
# Start/Stop Steam & Take/Capture a photo & Display Captured Photo
###################################################################

def start_camera_stream():
    global picam2, output, video_config
    picam2.configure(video_config)
    output = StreamingOutput()
    picam2.start_recording(JpegEncoder(), FileOutput(output))
    metadata = picam2.capture_metadata()
    time.sleep(1)

def stop_camera_stream():
    global picam2
    picam2.stop_recording()
    time.sleep(1)

##########################
# Take or Capture a photo
##########################

full_filename = None    # does not include file format at the end e.g. pimage_1718639639.jpg
image_name = None       # for example: pimage_1718639639

# Define the route for capturing a photo
@app.route('/capture_photo', methods=['POST'])
def capture_photo():
    try:
        take_photo()  # Call your take_photo function
        time.sleep(1)
        return jsonify(success=True, message="Photo captured successfully", captured_photo = prepare_image_filepath(image_name))
    except Exception as e:
        return jsonify(success=False, message=str(e))

def take_photo():
    global picam2, capture_settings
    try:
        timestamp = int(datetime.timestamp(datetime.now()))
        global image_name
        image_name = f'pimage_{timestamp}'
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], image_name)
        global full_filename                # get filepath for later for another function: show_captured_photo()
        full_filename = filepath            # get filepath for later for another function: show_captured_photo()
        request = picam2.capture_request()
        request.save("main", f'{filepath}.jpg')
        global current_image_name
        current_image_name = image_name
        if capture_settings["makeRaw"]:
            request.save_dng(f'{filepath}.dng')
        request.release()
        #selected_resolution = capture_settings["Resolution"]
        #resolution = capture_settings["available-resolutions"][selected_resolution]
        #original_image = Image.open(filepath)
        #resized_image = original_image.resize(resolution)
        #resized_image.save(filepath)
        logging.info(f"Image captured successfully. Path: {filepath}")
    except Exception as e:
        logging.error(f"Error capturing image: {e}")

def prepare_image_filepath(image_name):
    # captured photo must be at http://192.168.200.103:8001/static/gallery/pimage_1718639639.jpg
    # current full_filename: home/kipiuser/github/kipi/kipi_camera/static/gallery/pimage_1718639639
    # current image_name: pimage_1718639639
    image_filepath = '/static/gallery/' + image_name + '.jpg'
    return image_filepath

###################
# Configure Camera
###################

def configure_camera(live_settings):
    picam2.set_controls(live_settings)
    time.sleep(0.5)

def restart_configure_camera(restart_settings):
        stop_camera_stream()
        transform = Transform()
        # Update settings that require a restart
        for key, value in restart_settings.items():
            if key in restart_settings:
                if key in ('hflip', 'vflip'):
                    setattr(transform, key, value)
        video_config["transform"] = transform
        start_camera_stream()

#######################
# Send image to OpenAI
#######################

current_image_name = None
image_description = None

def send_image_to_openai():
    image_name = current_image_name
    image = None

    def prepare_image_name_for_openai(image_name):
        #image_name = image_name + ".jpg"
        image_name = (UPLOAD_FOLDER + "/" + image_name + ".jpg")
        print("Call function: prepare_image_name_for_openai | name of the image:" + image_name)
        return image_name

    def prepare_image_for_openai(image_name):
        image = cv2.imread(image_name)
        _, buffer = cv2.imencode(".jpg", image)
        base64Image = base64.b64encode(buffer).decode("utf-8")
        print("Call function: prepare_image_for_openai(image_name)")
        return base64Image

    def prepare_prompt_for_openai(base64Image):
        PROMPT_MESSAGES = [
            {
                "role": "user",
                "content": [
                    openai_prompt,
                    {"image": base64Image, "resize": 768},
                ],
            },
        ]
        params = {
            "model": "gpt-4o",
            "messages": PROMPT_MESSAGES,
            "max_tokens": 100,
        }
        return params

    def send_prompt_to_OpenAI(params):
        global open_ai_api_key
        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", open_ai_api_key))
        result = client.chat.completions.create(**params)
        description = result.choices[0].message.content
        print("------ OpenAI photo description: ------")
        print(description)
        return description

    # Start 'send image to OpenAI' routine
    image_name = prepare_image_name_for_openai(image_name)
    image = prepare_image_for_openai(image_name)
    params = prepare_prompt_for_openai(image)
    global image_description
    image_description = send_prompt_to_OpenAI(params)

# Define the route for sending an image to OpenAI
@app.route('/send_image_to_openai', methods=['POST'])
def send_image():
    try:
        send_image_to_openai()  # Call your send_image_to_openai function
        time.sleep(1)
        global image_description
        return jsonify(success=True, message="Image send to OpenAI successfully", photo_description=image_description)
    except Exception as e:
        return jsonify(success=False, message=str(e))

################
# Start the app
################

if __name__ == "__main__":
    # Configure logging
    logging.basicConfig(level=logging.INFO)  # Change the level to DEBUG for more detailed logging

    # Start Camera stream
    start_camera_stream()
    
    # Start the Flask development server and run the application
    app.run(debug=False, host='0.0.0.0', port=8001)