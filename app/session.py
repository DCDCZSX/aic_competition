import time

consultation_sessions = {}

class ConsultationSession:
    def __init__(self, session_id, medical_record, main_question):
        self.session_id = session_id
        self.medical_record = medical_record
        self.main_question = main_question
        self.progress = 0
        self.current_step = "初始化"
        self.identified_specialties = []
        self.specialist_results = {}
        self.final_conclusion = None
        self.status = "running"  # running, completed, error
        self.start_time = time.time()
        self.logs = []
    
    def update_progress(self, progress, step, message=""):
        self.progress = progress
        self.current_step = step
        log_entry = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "progress": progress,
            "step": step,
            "message": message
        }
        self.logs.append(log_entry)
        
        # 发送实时更新到前端
        try:
            from flask_socketio import SocketIO
            if 'socketio' in globals() and socketio:
                socketio.emit('progress_update', {
                    'session_id': self.session_id,
                    'progress': progress,
                    'step': step,
                    'message': message,
                    'timestamp': log_entry["timestamp"]
                })
        except ImportError:
            pass
        
        # 终端输出
        print(f"[{log_entry['timestamp']}] 进度: {progress}% - {step}: {message}")