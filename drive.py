import asyncio
import base64
import json
import time
from io import BytesIO
from multiprocessing import Process, Queue
import cv2
import numpy as np
import websockets
from PIL import Image
from lane_line_detection import calculate_control_signal, find_left_right_points, birdview_transform
from traffic_sign_detection import detect_sign, detect_distance, counter_car
import pickle
from ultralytics import YOLO
import statistics as st
import threading

###############################################################
config_path = './config_param.json'
with open(config_path) as config_buffer:
    config = json.loads(config_buffer.read())

HEIGHT = config['general']['height']
WIDTH = config['general']['width']
WIDTH_SIGN = config['general']['width_sign']
HEIGHT_SIGN = config['general']['height_sign']
model_detect_sign_path = config['throttle_04']['model_detect_sign_path']
model_detect_sign = YOLO(model_detect_sign_path)
##############################################################
#LOAD FUZZY FUNCTION FOR THROTTLE AND STEERING
with open(r'cds_fuzzy_logic/speed_func/normal_throttle_func.pkl', 'rb') as f:
    speed_function = pickle.load(f)

with open(r'cds_fuzzy_logic/steering_func/steering_func.pkl', 'rb') as f:
    steering_function = pickle.load(f)

with open(r'cds_fuzzy_logic/speed_func/lr_sign_func.pkl', 'rb') as f:
    lr_sign_function = pickle.load(f)

with open(r'cds_fuzzy_logic/speed_func/object_func.pkl', 'rb') as f:
    object_function = pickle.load(f)

with open(r'cds_fuzzy_logic/speed_func/stop_sign_func.pkl', 'rb') as f:
    stop_sign_function = pickle.load(f)

with open(r'cds_fuzzy_logic/speed_func/noentry_sign_func.pkl', 'rb') as f:
    noentry_sign_function = pickle.load(f)

with open(r'cds_fuzzy_logic/speed_func/straight_sign_func.pkl', 'rb') as f:
    straight_sign_function = pickle.load(f)
##############################################################

g_image_queue = Queue(maxsize=5)
sign_queue = Queue(maxsize=5)
car_queue = Queue(maxsize= 5)

def process_traffic_sign_loop(g_image_queue, sign_queue, car_queue):
    while True:
        if g_image_queue.empty():
            continue
        image = g_image_queue.get()
        # Prepare visualization image
        draw = image.copy()
        # Detect traffic signs
        sign, car = detect_sign(image, model_detect_sign, draw=draw)
        if sign:
            if not sign_queue.full():
                sign_queue.put(sign)
        if car:
            if not car_queue.full():
                car_queue.put(car)   
        # Show the result to a window
        cv2.imshow("Traffic signs", draw)
        cv2.waitKey(1)

distance_lst = []
sign_lst = []
async def process_image(websocket, path):
    async for message in websocket:
        global distance_lst, sign_lst
        ###### PREPROCESSING AND CALCULATE USEFUL VALUES 
        # Get image from simulation
        data = json.loads(message)
        image = Image.open(BytesIO(base64.b64decode(data["image"])))
        image = np.asarray(image)

        image_copy = image.copy().astype('float32')
        image_copy[:,:,0] -= 103.939
        image_copy[:,:,1] -= 116.779
        image_copy[:,:,2] -= 123.68
        image_lane = cv2.resize(image_copy, (WIDTH, HEIGHT))

        image_BGR = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        image_sign = cv2.resize(image_BGR, (WIDTH_SIGN, HEIGHT_SIGN))

        # Prepare visualization image
        image = cv2.resize(image, (WIDTH, HEIGHT))
        draw = image.copy()

        # Update image to g_image_queue - used to run sign detection
        if not g_image_queue.full():
            g_image_queue.put(image_sign)

        if not sign_queue.empty():
            signs = sign_queue.get()
        else:
            signs = []

        if not car_queue.empty():
            cars = car_queue.get()
        else:
            cars = []

        # Calculate the distance for signs
        distance = None

        if signs:
            sign = signs[-1][0]
            sign_lst.append(sign)
            signs_pos = signs[-1][:]
            signs_pos.pop(0)
            car_pos = [WIDTH_SIGN/2, HEIGHT_SIGN]
            distance = detect_distance(signs_pos, car_pos, WIDTH_SIGN, HEIGHT_SIGN)
            
            if len(distance_lst)>0:
                if distance < 0 and distance_lst[-1] <= 20:
                    distance = 0

            distance_lst.append(distance)
            
            if len(distance_lst) > 2 and distance_lst[-2] < distance_lst[-1]:
                distance_lst.pop(-1)
        
        if len(sign_lst) > 0 and len(distance_lst) > 0:    
            if len(signs) == 0 and sign_lst[-1] == 'stop' and distance_lst[-1] <= 20:
                distance_lst.append(0)

        if distance_lst:
            distance = distance_lst[-1]
        
        # Discard straight, no entry when go through
        if len(sign_lst) > 0:
            sign = st.mode(sign_lst)
            if (sign == 'straight' and distance < 20) or (sign == 'no_entry' and distance < 20):
                distance_lst =[]
                sign_lst = []

        # Calculate the distance for car (object)
        lst_car = []
        if cars:
            sign_pos = []
            number = "NO"
            if len(cars) == 1:
                sign_pos = cars[-1][:]
                sign_pos.pop(0)
                number = 'small'
            else:
                sign_pos = cars[-2:][:]
                for item in sign_pos:
                    item.pop(0)
                number = 'big'
                
            distance_car, right, left = counter_car(sign_pos, 320, 240, number)
            lst_car = [distance_car, right, left]
            
        ###### CAR CONTROLLER
        angle, check_discard, brake = calculate_control_signal(image_lane, signs, lst_car, distance, draw=draw)
        if check_discard == True:
             sign_lst = []
             distance_lst = []
        
        # Transform angle to range of 
        if angle > 90:
            angle = angle - 90
            steering = - steering_function(angle).item()
        elif angle <= 90:
            angle = 90 - angle
            steering = steering_function(angle).item()

        # Determine throttle by steering (normal road)
        throttle = speed_function(abs(steering)).item()
        
        # Using steering and distance to determine throttle (if has sign)
        if len(sign_lst) !=0:
            sign = st.mode(sign_lst)
            print(distance)
            distance = (distance - 0) / (200 - 0)

            if sign == 'right' or sign == 'left':
                throttle = lr_sign_function(steering, distance).item()
            if sign == 'stop':
                throttle = stop_sign_function(steering, distance).item()
                if distance == 0:
                    throttle = 0
            if sign  == 'noentry':
                throttle = straight_sign_function(steering, distance).item() # KHONG CO NO ENTRY
            if sign == 'straight':
                throttle = straight_sign_function(steering, distance).item()

        # Using steering and distance to determine throttle (if has object)        
        if len(lst_car) != 0:
            distance_car = lst_car[0]
            distance_car = (distance_car - 0) / (250 - 0)
            throttle = object_function(steering, distance_car).item()

        # Braking when it's in need
        speed = float(data['speed'])
        print(speed)      
        if brake and speed >= 25:
            throttle = 0
            print('phanh')

        cv2.imshow("draw", draw)
        cv2.waitKey(1)
        # Send back throttle and steering angle
        message = json.dumps(
            {"throttle": throttle, "steering": steering})
        print(message)
        await websocket.send(message)

async def main():
    async with websockets.serve(process_image, "0.0.0.0", 4567, ping_interval=None):
        await asyncio.Future()  # run forever

if __name__ == '__main__':
    p = Process(target=process_traffic_sign_loop, args=(g_image_queue, sign_queue, car_queue))
    p.start()
    asyncio.run(main())