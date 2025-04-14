from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
import heapq
import os

app = Flask(__name__, template_folder='templates', static_folder='static')
CORS(app)

# --- Constants & Global Variables ---
F = 1 << 14            # Fixed-point factor (17.14 format)
PRI_MIN = 0
PRI_MAX = 63
TIMER_FREQ = 100       # Ticks per second (for once-per-second updates)
TIME_SLICE = 4         # Time slice (in ticks)

tick_count = 0         # Global tick counter
load_avg = 0           # Global load_avg in fixed-point representation
threads = {}           # Dictionary of threads (keyed by tid)
ready_queue = []       # Heap for ready threads: ( -priority, counter, tid )
heap_counter = 0       # Tie-breaker counter for heap ordering

# --- Fixed-Point Helper Functions ---
def int_to_fixed(n):
    return n * F

def fixed_to_int(x):
    return x // F

def fixed_to_int_round(x):
    if x >= 0:
        return (x + F // 2) // F
    else:
        return (x - F // 2) // F

def fixed_mul(x, y):
    return (x * y) >> 14

def fixed_div(x, y):
    return (x << 14) // y

# --- Thread Class ---
class Thread:
    def __init__(self, tid, burst_time, nice=0):
        self.tid = tid
        self.burst_time = burst_time
        self.remaining_time = burst_time
        self.nice = nice  # In range -20 to 20
        self.recent_cpu = 0  # Fixed-point representation (initially 0)
        self.priority = self.calculate_priority()  # Computed per formula

    def calculate_priority(self):
        # Priority formula: PRI_MAX - (recent_cpu/4) - (2 * nice)
        recent_cpu_int = self.recent_cpu // F
        recent_cpu_div4 = recent_cpu_int // 4
        prio = PRI_MAX - recent_cpu_div4 - (2 * self.nice)
        # Clamp within allowed range.
        return max(PRI_MIN, min(PRI_MAX, prio))

    def update_priority(self):
        self.priority = self.calculate_priority()

# --- Ready Queue Helpers ---
def push_ready(thread):
    global heap_counter
    # Use negative priority for the min-heap
    heapq.heappush(ready_queue, (-thread.priority, heap_counter, thread.tid))
    heap_counter += 1

def rebuild_ready_queue():
    global ready_queue, heap_counter
    new_queue = []
    heap_counter = 0
    for t in threads.values():
        if t.remaining_time > 0:
            heapq.heappush(new_queue, (-t.priority, heap_counter, t.tid))
            heap_counter += 1
    ready_queue[:] = new_queue

# --- Scheduler Update Functions ---
def update_load_avg():
    global load_avg
    # Count threads that are ready (i.e. remaining_time > 0)
    ready_threads = sum(1 for t in threads.values() if t.remaining_time > 0)
    # load_avg = (59/60)*load_avg + (1/60)*ready_threads
    load_avg = ((59 * load_avg) + (ready_threads * F)) // 60

def update_recent_cpu_all():
    for t in threads.values():
        # Coefficient = 2*load_avg / (2*load_avg + 1)
        coeff = fixed_div(2 * load_avg, (2 * load_avg + F))
        t.recent_cpu = fixed_mul(coeff, t.recent_cpu) + int_to_fixed(t.nice)
        t.update_priority()

def simulate_tick(running_thread):
    global tick_count
    tick_count += 1
    # Increment recent_cpu for the running thread (in fixed-point)
    running_thread.recent_cpu += int_to_fixed(1)
    # Decrement the thread's remaining time
    running_thread.remaining_time -= 1

    # Every 4 ticks, recalc priorities for all threads and rebuild queue.
    if tick_count % TIME_SLICE == 0:
        for t in threads.values():
            if t.remaining_time > 0:
                t.update_priority()
        rebuild_ready_queue()

    # Every TIMER_FREQ ticks (once per second), update load_avg and recent_cpu.
    if tick_count % TIMER_FREQ == 0:
        update_load_avg()
        update_recent_cpu_all()
        rebuild_ready_queue()

# --- After-Request Hook for JSON Responses ---
@app.after_request
def add_charset(response):
    if response.content_type.startswith("application/json"):
        response.headers["Content-Type"] = "application/json; charset=utf-8"
    return response

# --- Flask Endpoints ---

# Add a new thread
@app.route('/add_thread', methods=['POST'])
def add_thread():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400
    tid = data.get('tid')
    burst_time = data.get('burst_time')
    nice = data.get('nice', 0)
    if tid is None or burst_time is None:
        return jsonify({"error": "Missing required fields"}), 400
    if not isinstance(burst_time, int) or burst_time <= 0:
        return jsonify({"error": "Burst time must be a positive integer"}), 400
    if not isinstance(nice, int) or not (-20 <= nice <= 20):
        return jsonify({"error": "Nice value must be between -20 and 20"}), 400

    thread = Thread(tid, burst_time, nice)
    threads[tid] = thread
    push_ready(thread)
    return jsonify({"message": f"Thread {tid} added successfully!"}), 200

# Set thread's nice value and recalc its priority.
@app.route('/set_thread_nice', methods=['POST'])
def set_thread_nice():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400
    tid = data.get('tid')
    new_nice = data.get('nice')
    if tid is None or new_nice is None:
        return jsonify({"error": "Missing required fields"}), 400
    if tid not in threads:
        return jsonify({"error": "Thread not found"}), 404
    if not isinstance(new_nice, int) or not (-20 <= new_nice <= 20):
        return jsonify({"error": "Nice value must be between -20 and 20"}), 400

    thread = threads[tid]
    thread.nice = new_nice
    thread.update_priority()
    rebuild_ready_queue()
    return jsonify({"message": f"Thread {tid} nice value set to {new_nice}"}), 200

# Return the thread's nice value.
@app.route('/get_thread_nice/<tid>', methods=['GET'])
def get_thread_nice(tid):
    if tid not in threads:
        return jsonify({"error": "Thread not found"}), 404
    return jsonify({"tid": tid, "nice": threads[tid].nice}), 200

# Return 100 times the thread's recent_cpu (rounded).
@app.route('/get_recent_cpu/<tid>', methods=['GET'])
def get_recent_cpu(tid):
    if tid not in threads:
        return jsonify({"error": "Thread not found"}), 404
    value = fixed_to_int_round(threads[tid].recent_cpu * 100)
    return jsonify({"tid": tid, "recent_cpu": value}), 200

# Return 100 times the current system load average.
@app.route('/get_load_avg', methods=['GET'])
def get_load_avg():
    value = fixed_to_int_round(load_avg * 100)
    return jsonify({"load_avg": value}), 200

# Run the scheduler simulation.
@app.route('/run_scheduler', methods=['GET'])
def run_scheduler():
    global tick_count
    schedule_log = []
    # Run until every thread finishes.
    while any(t.remaining_time > 0 for t in threads.values()):
        rebuild_ready_queue()
        if not ready_queue:
            break
        # Get the thread with the highest priority.
        _, _, tid = heapq.heappop(ready_queue)
        current_thread = threads[tid]
        # Run for a time slice or until the thread completes.
        slice_time = min(TIME_SLICE, current_thread.remaining_time)
        for _ in range(slice_time):
            simulate_tick(current_thread)
            schedule_log.append({
                "tick": tick_count,
                "tid": tid,
                "priority": current_thread.priority,
                "remaining_time": current_thread.remaining_time
            })
            if current_thread.remaining_time <= 0:
                break
        # If thread still has work left, put it back in the ready queue.
        if current_thread.remaining_time > 0:
            push_ready(current_thread)
    return jsonify({"schedule": schedule_log}), 200

# Reset the simulation.
@app.route('/reset', methods=['GET'])
def reset():
    global tick_count, load_avg, threads, ready_queue, heap_counter
    tick_count = 0
    load_avg = 0
    threads = {}
    ready_queue = []
    heap_counter = 0
    return jsonify({"message": "Simulation reset successfully!"}), 200

# Serve the frontend.
@app.route('/')
def index():
    return render_template('index.html')

# Ensure the required directories exist.
if not os.path.exists('templates'):
    os.makedirs('templates')
    
if not os.path.exists('static'):
    os.makedirs('static')

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
