"""Server file to broadcast live stream to the connected clients."""

import cv2
import errno
import socket
import struct
import sys
import time
import traceback
from threading import Thread

sys.path.insert(0, '../include')
import helper

args = helper.parser.parse_args()
max_concurrent_clients = 5    # total active clients possible at once

# If the reader lags behind the writer by lag_threshold number of payloads,
# it is synchronized with the writer.
lag_threshold = 10

# Initialize a constant-sized list of payloads.
# payload_list = [None] * max_payload_count  # TODO: needs to be fixed
payload_list = [None] * (lag_threshold + 1)


class Server:

    def __init__(self, lag_threshold, max_payload_count):
        self.lag_threshold = lag_threshold
        self.max_payload_count = max_payload_count
        self.last_written_index = 0    # index at which the writer wrote last
        self.payload_count = 0    # number of payloads sent over socket

    def startWebcam(self):
        """Starts the stream and adjusts the screen resolution."""
        try:
            cap = cv2.VideoCapture(0)
            frame_width = 160
            frame_height = 120
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, frame_width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, frame_height)
            return cap
        except Exception as e:
            print(traceback.print_exc())

    def webcamFeed(self, cap):
        """Constructs a payload from the frames collected from the webcam and
        inserts them into a global list.
        """
        global payload_list
        payload = bytearray()
        frame_count = 0    # number of frames delivered
        write_index = 0    # most recently updated index of the message queue

        while True:
            ret, frame = cap.read()

            # Convert the frame into a byte string
            hashed_frame_dim = 0
            frame_dims = list(frame.shape)
            for dim in frame_dims:
                hashed_frame_dim <<= 16
                hashed_frame_dim += dim

            payload.extend(struct.pack('Q', hashed_frame_dim))
            payload.extend(frame.tobytes())
            #  payload.append(struct.pack('Q', hashed_frame_dim))
            #  payload.append()
            frame_count += 1

            # Each payload comprises of 'frames_per_payload' number of the
            # following structures -
            #
            #       +------------------+---------------+
            #       | Frame dimensions |     Frame     |
            #       |     (Hashed)     | (byte string) |
            #       +------------------+---------------+
            #
            # Collect 'helper.frames_per_payload' number of frames before starting
            # the transfer.
            if frame_count == helper.frames_per_payload:
                # Update the list of payloads.
                payload_list[write_index] = payload

                if __debug__:
                    print('Populating index', write_index)

                self.last_written_index = write_index
                write_index = (write_index + 1) % self.max_payload_count
                payload = bytearray()
                frame_count = 0

    def handleConnection(self, connection, client_address, thread_id):
        """Handles an individual client connection."""
        global q    # TODO: inspect this

        served_payloads = 0

        # The instant when the consumer was created, it should start broadcasting
        # frames which were generated closest to that instant. 'last_written_index'
        # is used to keep track of this.
        index = self.last_written_index

        # Check if the writer has populated the index'th entry of the list. This is
        # only useful for the case if the client was started before the server
        # could entirely populate 'payload_list'.
        if not payload_list[index]:
            index = 0

        # wait_for_writer represents the duration for which consumer waits on the
        # webcam thread to fill up the queue with payloads.
        wait_for_writer = helper.frames_per_payload / 10.0

        print('Thread %d: Connection from %s' % (thread_id, client_address))
        print('Thread %d: Starting broadcast' % thread_id)

        try:
            while True:
                # Ensure that the reader is always behind the writer.
                if index == self.last_written_index:
                    if __debug__:
                        print('Covered up, waiting for writer')
                    time.sleep(wait_for_writer)
                else:
                    # Evaluate the lag between the reader and the writer. If the
                    # lag is more than the acceptable value, synchronize the reader
                    # by skipping the intermediate payloads and serve the most
                    # recently generated one.
                    if self.last_written_index >= index:
                        lag = self.last_written_index - index
                    else:
                        lag = self.max_payload_count - index + self.last_written_index

                    if lag >= self.lag_threshold:
                        index = self.last_written_index

                    if __debug__:
                        print('Sending index', index)

                    self.payload_count += 1
                    connection.sendall(payload_list[index])
                    served_payloads += 1
                    index = (index + 1) % self.max_payload_count

        except socket.error as e:
            if isinstance(e.args, tuple):
                if e[0] == errno.EPIPE:
                    print >> sys.stderr, "Client disconnected"
                else:
                    # TODO: Handle other socket errors
                    pass
            else:
                print >> sys.stderr, "socket error:", e

        except IOError as e:
            print >> sys.stderr, "IOError:", e

    def serverStatistics(self):
        """Logs data for tracking server performance."""
        print "\nServer statistics" \
            + "\n-----------------"
        print "Payloads delivered:", self.payload_count

    def cleanup(self, connection):
        """Closes the connection and performs cleanup."""
        if connection:
            cv2.destroyAllWindows()
            print "Closing the socket"
            connection.close()
            self.serverStatistics()


def main():
    """Sets up a listening socket for incoming connections."""
    connection = None
    reader_count = 0    # number of active readers (clients)

    # Create a TCP/IP socket.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # Bind the socket to a port.
    server_address = (args.server_ip, args.port)
    print "Starting up on %s:%s" % server_address
    sock.bind(server_address)

    # Listen for incoming connections.
    sock.listen(max_concurrent_clients)

    # If the reader lags behind the writer by lag_threshold number of payloads,
    # it is synchronized with the writer.
    lag_threshold = 10

    # At any given instant, the gap between a reader and the writer can be
    # atmost lag_threshold. This is also the lower bound on the size of the
    # payload buffer.
    max_payload_count = lag_threshold + 1

    server = Server(lag_threshold, max_payload_count)
    # Start the webcam.
    cap = server.startWebcam()

    # Start a thread to collect frames and generate payload to be served.
    webcam_thread = Thread(target=server.webcamFeed, args=(cap,))
    webcam_thread.setDaemon(True)
    webcam_thread.start()

    try:
        while True:
            print('Thread %d: Waiting for a connection' % reader_count)
            connection, client_address = sock.accept()

            # Start a consumer thread corresponding to each connected client.
            consumer_thread = Thread(
                target=server.handleConnection,
                args=(connection, client_address, reader_count))
            reader_count += 1
            consumer_thread.setDaemon(True)
            consumer_thread.start()

    except KeyboardInterrupt:
        sys.exit("Exiting.")

    finally:
        server.cleanup(connection)


if __name__ == "__main__":
    main()
