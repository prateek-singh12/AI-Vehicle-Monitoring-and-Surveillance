import cv2
from time import time
import numpy as np
from ultralytics.solutions.solutions import BaseSolution
from ultralytics.utils.plotting import Annotator, colors
from datetime import datetime
import mysql.connector
from paddleocr import PaddleOCR
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

class SpeedEstimator(BaseSolution):
    def __init__(self, socketio=None, **kwargs):
        super().__init__(**kwargs)
        self.initialize_region()
        self.spd = {}  
        self.trkd_ids = []
        self.trk_pt = {}  
        self.trk_pp = {} 
        self.logged_ids = set()
        self.ocr = PaddleOCR(use_angle_cls=True, lang='en')
        self.db_connection = self.connect_to_db()
        self.speed_threshold = 50  
        self.email_sender = "pspk123999@gmail.com"
        self.email_password = "vsnabwjfhxjgxwge"
        self.email_receiver = "pspk123999@gmail.com"
        self.socketio = socketio  

    def connect_to_db(self):
        try:
            connection = mysql.connector.connect(
                host="localhost",
                user="root",
                password="prateek",
                database="numberplates_speed"
            )
            return connection
        except mysql.connector.Error as err:
            print(f"Error connecting to database: {err}")
            return None

    def perform_ocr(self, image_array):
        if isinstance(image_array, np.ndarray):
            results = self.ocr.ocr(image_array, rec=True)
            if results and results[0]:
                return ' '.join([result[1][0] for result in results[0]])
        return ""

    def save_to_database(self, date, time, track_id, class_name, speed, numberplate, status=""):
        if not self.db_connection:
            print("Database connection not available. Skipping save.")
            return
        try:
            cursor = self.db_connection.cursor()
            query = """
                INSERT INTO my_data (date, time, track_id, class_name, speed, numberplate, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """
            cursor.execute(query, (date, time, track_id, class_name, speed, numberplate.replace(" ", ""), status))
            self.db_connection.commit()
        except mysql.connector.Error as err:
            print(f"Error saving to database: {err}")

    def is_blacklisted(self, numberplate):
        if not self.db_connection or not numberplate:
            return False
        try:
            cursor = self.db_connection.cursor()
            query = "SELECT EXISTS(SELECT 1 FROM blacklisted_vehicles WHERE numberplate = %s)"
            cursor.execute(query, (numberplate,))
            result = cursor.fetchone()
            return result[0] == 1
        except Exception as e:
            print(f"Blacklist check error for {numberplate}: {str(e)}")
            return False

    def send_email(self, numberplate, speed, status):
        try:
            msg = MIMEMultipart()
            msg['From'] = self.email_sender
            msg['To'] = self.email_receiver
            msg['Subject'] = f"Vehicle Violation Alert: {status}"

            body = f"""
            A vehicle violation has been detected:
            Number Plate: {numberplate}
            Speed: {speed} km/h
            Status: {status}
            Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            """
            msg.attach(MIMEText(body, 'plain'))

            with smtplib.SMTP('smtp.gmail.com', 587) as server:
                server.starttls()
                server.login(self.email_sender, self.email_password)
                server.sendmail(self.email_sender, self.email_receiver, msg.as_string())
            print(f"Email sent for {status} vehicle: {numberplate}")
        except Exception as e:
            print(f"Failed to send email: {str(e)}")

    def get_threshold_speed(self):
        try:
            cursor = self.db_connection.cursor()
            cursor.execute("SELECT threshold_speed FROM settings WHERE id = 1")
            result = cursor.fetchone()
            return result[0] if result else 50.0
        except Exception as e:
            print(f"Threshold fetch error: {str(e)}")
            return 50.0

    def estimate_speed(self, im0):
        self.annotator = Annotator(im0, line_width=self.line_width)
        self.extract_tracks(im0)
        current_time = datetime.now()
        results = []

        threshold_speed = self.get_threshold_speed()
        print(f"Current threshold: {threshold_speed} km/h")

        for box, track_id, cls in zip(self.boxes, self.track_ids, self.clss):
            x1, y1, x2, y2 = map(int, box)
            cropped_image = np.array(im0)[y1:y2, x1:x2]
            ocr_text = self.perform_ocr(cropped_image).strip().replace(" ", "")
            class_name = self.names[int(cls)]

            if track_id not in self.trk_pt:
                self.trk_pt[track_id] = time()
                self.trk_pp[track_id] = box

            current_time_sec = time()
            time_diff = current_time_sec - self.trk_pt[track_id]

            if track_id not in self.spd and time_diff > 0.05:  
                prev_center = np.array([(self.trk_pp[track_id][0] + self.trk_pp[track_id][2]) / 2,
                                        (self.trk_pp[track_id][1] + self.trk_pp[track_id][3]) / 2])
                curr_center = np.array([(x1 + x2) / 2, (y1 + y2) / 2])

                distance_moved = np.linalg.norm(curr_center - prev_center)
                avg_speed_kmh = (distance_moved / time_diff) * 3.6
                self.spd[track_id] = round(avg_speed_kmh, 2)

            speed = self.spd.get(track_id, 0)
            self.trk_pt[track_id] = current_time_sec
            self.trk_pp[track_id] = box

            print(f"Track ID {track_id} | Speed: {speed} km/h | Plate: {ocr_text}")

            color = (0, 128, 0)
            status = ""
            text = f"{ocr_text} | {speed} km/h"

            if ocr_text and self.is_blacklisted(ocr_text):
                print(f"BLACKLIST DETECTED: {ocr_text}")
                color = (0, 0, 255)
                status = "BLACKLISTED"
                text = f"{ocr_text} | BLACKLISTED | {speed} km/h"
            elif speed > threshold_speed:
                color = (255, 0, 0)
                status = "OVER SPEED"
                text = f"{ocr_text} | OVER SPEED | {speed} km/h"

            self.annotator.box_label((x1, y1, x2, y2), text, color=color)

            
            if self.socketio and ocr_text:
                self.socketio.emit('detection_update', {
                    'track_id': track_id,
                    'numberplate': ocr_text,
                    'speed': speed,
                    'status': status
                }, namespace='/')

            if track_id not in self.logged_ids and ocr_text and track_id in self.spd:
                self.save_to_database(
                    current_time.strftime("%Y-%m-%d"),
                    current_time.strftime("%H:%M:%S"),
                    track_id,
                    class_name,
                    speed,
                    ocr_text,
                    status
                )
                if status in ["BLACKLISTED", "OVER SPEED"]:
                    self.send_email(ocr_text, speed, status)
                self.logged_ids.add(track_id)

            results.append({
                "track_id": track_id,
                "class_name": class_name,
                "speed": speed,
                "numberplate": ocr_text,
                "status": status
            })

        return results