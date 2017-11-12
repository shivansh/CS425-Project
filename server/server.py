"""Server file to serve files requested by the connecting clients."""

import cv2
import errno
import socket
import struct
import sys
import time
from threading import Thread
from Queue import Queue

sys.path.insert(0, '../include')
import helper

# Define the globals
args = helper.parser.parse_args()
max_concurrent_clients = 5  # Total active clients possible at once.

# TODO Write logic behind this limit.
max_payload_count = 10 * max_concurrent_clients

# If the reader lags behind the writer by 'lag_threshold'
# number of payloads, it is synchronized with the writer.
lag_threshold = 5

# Initialize a constant-sized list of payloads.
payload_list = [None] * max_payload_count

# Start stream and adjust screen resolution.
cap = cv2.VideoCapture(0)
frame_width = 160
frame_height = 120
cap.set(cv2.CAP_PROP_FRAME_WIDTH, frame_width)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, frame_height)

connection = None
last_written_index = 0     # Index at which the writer wrote last.
consumer_thread_count = 0  # Number of active clients.
payload_count = 0          # Number of payloads sent over network.

def WebcamFeed():
    """Constructs a payload from the frames collected from
    the webcam and inserts them into a global list.
    """
    global payload_list, last_written_index
    payload = ""
    frame_count = 0
    write_index = 0

    while True:
        ret, frame = cap.read()

        # Serialize the frames
        hashed_frame_dim = 0
        frame_dims = list(frame.shape)
        for dim in frame_dims:
            hashed_frame_dim <<= 16
            hashed_frame_dim += dim

        payload += struct.pack('Q', hashed_frame_dim) + frame.tobytes()
        frame_count += 1

        # Each payload comprises of 'frames_per_payload'
        # number of the following structures -
        #       +------------------+---------------+
        #       | Frame dimensions |     Frame     |
        #       |     (Hashed)     | (byte string) |
        #       +------------------+---------------+
        #
        # Collect 'helper.frames_per_payload' number of
        # frames before starting the transfer.
        if frame_count == helper.frames_per_payload:
            # Update the list of payloads.
            payload_list[write_index] = payload

            if __debug__:
                print 'Populating index', write_index

            last_written_index = write_index
            write_index = (write_index+1) % max_payload_count
            payload = ""
            frame_count = 0

def HandleConnection(connection, client_address, thread_id):
    """Handles an individual client connection."""
    global q, payload_count

    # The instant when the consumer was created, it should start
    # broadcasting frames which were generated closest to that
    # instant. 'last_written_index' is used to keep track of this.
    index = last_written_index

    # Check if the writer has populated the index'th entry of the
    # list. This is only useful for the case if the client was started
    # before the server could entirely populate 'payload_list'.
    if not payload_list[index]:
        index = 0

    # Duration for which consumer waits on the webcam
    # thread to fill up the queue with payloads.
    wait_for_writer = helper.frames_per_payload / 10.0

    print 'Thread %d: Connection from %s' % (thread_id, client_address)
    print 'Thread %d: Starting broadcast' % thread_id

    served_payloads = 0

    try:
        while True:
            # Ensure that the reader is always behind the writer.
            if index == last_written_index:
                if __debug__:
                    print 'Covered up, waiting for writer'
                time.sleep(wait_for_writer)
            else:
                # Evaluate the lag between the reader and the writer.
                if (last_written_index - index) >= 0:
                    lag = last_written_index - index
                else:
                    lag = max_payload_count - index + last_written_index

                if lag >= lag_threshold:
                    index = last_written_index

                if __debug__:
                    print 'Sending index', index

                payload_count += 1
                connection.sendall(payload_list[index])
                served_payloads += 1
                index = (index+1) % max_payload_count

    except socket.error, e:
        if isinstance(e.args, tuple):
            if e[0] == errno.EPIPE:
                print >> sys.stderr, 'Client disconnected'
            else:
                # TODO Handle other socket errors.
                pass
        else:
            print >> sys.stderr, 'Socket error', e

    except IOError, e:
        print >> sys.stderr, 'IOError:', e

def Cleanup(connection):
    """Closes the connection and performs cleanup."""
    if connection:
        cv2.destroyAllWindows()
        print 'Closing the socket'
        connection.close()
        ServerStatistics()

def ServerStatistics():
    """Logs data for tracking server performance."""
    print '\nServer statistics' \
        + '\n-----------------'
    print 'Payloads delivered:', payload_count
    print ''

# Create a TCP/IP socket.
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

# Bind the socket to a port.
server_address = ('localhost', args.port)
print 'Starting up on %s:%s' % server_address
sock.bind(server_address)

# Listen for incoming connections.
sock.listen(max_concurrent_clients)

# Start a thread to collect frames and generate
# payload to be served to the clients.
webcam_thread = Thread(target = WebcamFeed)
webcam_thread.setDaemon(True)
webcam_thread.start()

try:
    while True:
        print 'Thread %d: Waiting for a connection' % consumer_thread_count
        connection, client_address = sock.accept()

        # Start a consumer thread corresponding to
        # each connected client.
        consumer_thread = Thread(target = HandleConnection,
                                 args = (connection,
                                         client_address,
                                         consumer_thread_count))
        consumer_thread_count += 1
        consumer_thread.setDaemon(True)
        consumer_thread.start()

except KeyboardInterrupt:
    sys.exit("KeyboardInterrupt encountered")

finally:
    Cleanup(connection)
