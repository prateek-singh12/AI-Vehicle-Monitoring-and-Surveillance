from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO
import os
import cv2
import numpy as np
import mysql.connector
from main import SpeedEstimator  
app = Flask(__name__)
socketio = SocketIO(app) 

UPLOAD_FOLDER = "uploads"
RESULT_FOLDER = "results"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(RESULT_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

model_path = "models/best.pt"

def generate_output_video(input_video_path, socketio):
    """Processes video and saves the output with detections and speed estimations, emitting real-time updates."""
    cap = cv2.VideoCapture(input_video_path)
    if not cap.isOpened():
        print("Error: Unable to open video file.")
        return None

    frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = int(cap.get(cv2.CAP_PROP_FPS)) or 30  
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    current_frame = 0

    output_path = os.path.join(RESULT_FOLDER, "output.mp4")
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out_writer = cv2.VideoWriter(output_path, fourcc, fps, (frame_width, frame_height))

    estimator = SpeedEstimator(region=[(0, 145), (1018, 145)], model=model_path, line_width=2, socketio=socketio)

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        current_frame += 1
        
        if total_frames > 0:
            progress = (current_frame / total_frames) * 100
            socketio.emit('progress_update', {'progress': progress}, namespace='/')
        else:
            socketio.emit('progress_update', {'progress': -1}, namespace='/')  

        detection_results = estimator.estimate_speed(frame)
        processed_frame = estimator.annotator.result()

        if processed_frame is not None and isinstance(processed_frame, np.ndarray):
            out_writer.write(processed_frame)

    cap.release()
    out_writer.release()
    
    socketio.emit('progress_update', {'progress': 100}, namespace='/')
    return output_path

def connect_to_db():
    try:
        return mysql.connector.connect(
            host="localhost",
            user="root",
            password="prateek",
            database="numberplates_speed",
            port=3306
        )
    except mysql.connector.Error as err:
        print(f"Database connection failed: {err}")
        return None

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=["POST"])
def upload_video():
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file uploaded"}), 400
            
        file = request.files['file']
        if not file or file.filename == '':
            return jsonify({"error": "No selected file"}), 400

        file_path = os.path.join(app.config["UPLOAD_FOLDER"], file.filename)
        file.save(file_path)
        print(f"File saved to: {file_path}")

        result_path = generate_output_video(file_path, socketio)
        if not result_path:
            return jsonify({"error": "Video processing failed"}), 500

        return jsonify({
            "message": "Video processed successfully",
            "result_video": result_path
        })

    except Exception as e:
        print(f"Upload error: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

# Blacklist Management 
@app.route('/blacklist', methods=["POST"])
def manage_blacklist():
    data = request.get_json()
    action = data.get('action')
    numberplate = data.get('numberplate').replace(" ", "")

    if action == 'add':
        return add_to_blacklist(numberplate)
    elif action == 'remove':
        return remove_from_blacklist(numberplate)
    else:
        return jsonify({"error": "Invalid action"}), 400

def add_to_blacklist(numberplate):
    try:
        estimator = SpeedEstimator()  
        db_connection = estimator.connect_to_db()
        cursor = db_connection.cursor()
        query = "INSERT INTO blacklisted_vehicles (numberplate, reason) VALUES (%s, %s)"
        cursor.execute(query, (numberplate, "Added via API"))
        db_connection.commit()
        return jsonify({"message": f"{numberplate} added to blacklist"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def remove_from_blacklist(numberplate):
    try:
        estimator = SpeedEstimator()  
        db_connection = estimator.connect_to_db()
        cursor = db_connection.cursor()
        query = "DELETE FROM blacklisted_vehicles WHERE numberplate = %s"
        cursor.execute(query, (numberplate,))
        db_connection.commit()
        return jsonify({"message": f"{numberplate} removed from blacklist"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Speed Threshold 
@app.route('/threshold', methods=['POST'])
def set_threshold():
    data = request.get_json()
    if not data or 'threshold' not in data:
        return jsonify({"error": "Invalid request format"}), 400
    
    threshold = data['threshold']
    
    try:
        conn = connect_to_db()
        if not conn:
            return jsonify({"error": "Database connection failed"}), 500
            
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                id INT PRIMARY KEY AUTO_INCREMENT,
                threshold_speed FLOAT NOT NULL DEFAULT 50
            )
        """)
        
        cursor.execute("INSERT INTO settings (threshold_speed) SELECT 50 WHERE NOT EXISTS (SELECT * FROM settings)")
        
        cursor.execute("UPDATE settings SET threshold_speed = %s WHERE id = 1", (float(threshold),))
        conn.commit()
        
        return jsonify({"message": f"Threshold updated to {threshold} km/h"})
        
    except ValueError:
        return jsonify({"error": "Invalid threshold value"}), 400
    except mysql.connector.Error as err:
        print(f"MySQL Error: {err}")
        return jsonify({"error": "Database operation failed"}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

# Analytics Dashboard 
@app.route('/stats', methods=['GET'])
def get_stats():
    try:
        conn = connect_to_db()
        if not conn:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = conn.cursor(dictionary=True)

        cursor.execute("SELECT COUNT(*) as total_vehicles FROM my_data")
        total_vehicles = cursor.fetchone()['total_vehicles']

        cursor.execute("SELECT AVG(speed) as average_speed FROM my_data")
        avg_speed_result = cursor.fetchone()['average_speed']
        average_speed = round(avg_speed_result, 2) if avg_speed_result else 0.0

        cursor.execute("SELECT COUNT(*) as overspeeding FROM my_data WHERE status = 'OVER SPEED'")
        overspeeding = cursor.fetchone()['overspeeding']

        cursor.execute("SELECT COUNT(*) as blacklisted FROM my_data WHERE status = 'BLACKLISTED'")
        blacklisted = cursor.fetchone()['blacklisted']

        cursor.execute("""
            SELECT numberplate, COUNT(*) as violation_count 
            FROM my_data 
            WHERE status IN ('OVER SPEED', 'BLACKLISTED') 
            GROUP BY numberplate 
            ORDER BY violation_count DESC 
            LIMIT 5
        """)
        top_violators = cursor.fetchall()

        return jsonify({
            "total_vehicles": total_vehicles,
            "average_speed": average_speed,
            "overspeeding": overspeeding,
            "blacklisted": blacklisted,
            "top_violators": top_violators
        })

    except mysql.connector.Error as err:
        print(f"MySQL Error: {err}")
        return jsonify({"error": "Database operation failed"}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

if __name__ == "__main__":
    socketio.run(app, debug=True)