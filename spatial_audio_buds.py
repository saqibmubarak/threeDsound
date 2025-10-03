import asyncio

import sounddevice as sd
import numpy as np
import pyquaternion as pq
import slab
import soundfile as sf
import threading
import queue
import time
import os
import struct
import bluetooth
import socket

# --- Configuration Parameters ---
# Path to your HRTF SOFA file.
HRTF_SOFA_FILE = 'dtf_las_nh4.sofa'

# Path to your music file (e.g., .wav, .flac, .ogg)
MUSIC_FILE_PATH = 'sample.mp3'

# --- Galaxy Buds Bluetooth Configuration ---
# Replace with your Galaxy Buds' Bluetooth MAC address
GALAXY_BUDS_MAC_ADDRESS = "a0:b0:bd:ed:62:f9"

# Based on your device discovery, the correct port is 2.
GALAXY_BUDS_RFCOMM_PORT = 2

# Audio stream parameters
SAMPLERATE = 44100
BLOCKSIZE = 1024
CHANNELS = 2
DTYPE = 'float32'

# --- Global State Variables ---
head_orientation_queue = queue.Queue(maxsize=10)
current_head_orientation = pq.Quaternion()
hrtf = None
audio_file = None
stop_event = threading.Event()


# --- Helper Functions ---

def crc16(data: bytes):
    """
    Calculates the CRC16-CCITT (XMODEM) checksum.
    """
    crc = 0x0000
    polynomial = 0x1021
    for byte in data:
        crc ^= (byte << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ polynomial
            else:
                crc = crc << 1
    return crc & 0xFFFF


def load_hrtf(file_path):
    """Loads HRTF data from a SOFA file."""
    print(f"Loading HRTF from: {file_path}...")
    try:
        loaded_hrtf = slab.HRTF(file_path)
        print("HRTF loaded successfully.")
        return loaded_hrtf
    except Exception as e:
        print(f"Error loading HRTF: {e}")
        return None


def load_audio_file(file_path):
    """Loads an audio file for playback."""
    print(f"Loading audio file: {file_path}...")
    try:
        loaded_audio = sf.SoundFile(file_path, 'r')
        print(f"Audio file loaded. Sample rate: {loaded_audio.samplerate}, Channels: {loaded_audio.channels}")
        return loaded_audio
    except Exception as e:
        print(f"Error loading audio file: {e}")
        return None


def discover_and_list_bluetooth_devices():
    """Scans for nearby Bluetooth devices and lists their RFCOMM services."""
    print("\n--- Discovering Bluetooth Devices (This may take a moment)... ---")
    nearby_devices = bluetooth.discover_devices(duration=8, lookup_names=True, flush_cache=True, lookup_class=False)
    if not nearby_devices:
        print("No Bluetooth devices found. Ensure Bluetooth is enabled and devices are discoverable.")
        return
    print(f"Found {len(nearby_devices)} nearby devices:")
    for addr, name in nearby_devices:
        print(f"  MAC: {addr}, Name: {name}")
        print(f"    Attempting to find RFCOMM services for {name} ({addr})...")
        try:
            services = bluetooth.find_service(address=addr)
            if services:
                for svc in services:
                    if "protocol" in svc and svc["protocol"] == "RFCOMM":
                        print(f"      Service: {svc['name']}, Protocol: {svc['protocol']}, Port: {svc['port']}")
            else:
                print("      No RFCOMM services found.")
        except bluetooth.BluetoothError as e:
            print(f"      Error finding services for {name}: {e}")
    print("------------------------------------------------------------------\n")


# --- Real Head Tracking with PyBluez ---

def send_enable_spatial_audio_command(sock):
    """
    Sends the command to enable spatial audio data streaming.
    Preamble (0xAA), Message ID (0xA8), Payload Length (0x01 0x00), Payload (0x01 for enable).
    """
    command = b'\xAA\xA8\x01\x00\x01'
    try:
        sock.send(command)
        print(f"Sent enable spatial audio command: {command.hex()}")
    except bluetooth.BluetoothError as e:
        print(f"Failed to send enable command: {e}")
    except Exception as e:
        print(f"Unexpected error sending command: {e}")


def parse_galaxy_buds_head_tracking_data(data_bytes):
    """
    Parses raw byte data from Galaxy Buds according to the reversed protocol.
    Expected format: [0xFE] [0x27] [Payload Len LSB] [Payload Len MSB] [0xA8] [QuatX] [QuatY] [QuatZ] [QuatW] [CRC16 LSB] [CRC16 MSB]
    """
    # Packet has a fixed total length of 23 bytes for spatial data
    EXPECTED_FULL_PACKET_LENGTH = 23
    HEADER_CRC_LEN = 6  # Preamble(1) + MsgID(1) + PayloadLen(2) + CRC(2)
    SUB_MSG_ID = 0xA8
    SPATIAL_MSG_ID = 0x27
    PREAMBLE = 0xFE

    if len(data_bytes) != EXPECTED_FULL_PACKET_LENGTH:
        return None

    if data_bytes[0] != PREAMBLE:
        return None

    message_id = data_bytes[1]
    if message_id != SPATIAL_MSG_ID:
        print(
            f"Received valid preamble ({PREAMBLE:02X}), but unexpected Message ID: {message_id:02X}. Raw: {data_bytes.hex()}")
        return None

    try:
        payload_length = struct.unpack('<H', data_bytes[2:4])[0]
        if payload_length != 17:  # SubMsgID(1) + Quaternion(16)
            return None

        sub_msg_id = data_bytes[4]
        if sub_msg_id != SUB_MSG_ID:
            return None

        quaternion_raw_bytes = data_bytes[5:21]
        x, y, z, w = struct.unpack('<ffff', quaternion_raw_bytes)

        received_crc = struct.unpack('<H', data_bytes[21:23])[0]
        calculated_crc = crc16(data_bytes[:21])

        if received_crc != calculated_crc:
            return None

        return pq.Quaternion(w=w, x=x, y=y, z=z)
    except (struct.error, IndexError):
        return None


def galaxy_buds_head_tracking_thread(mac_address, rfcomm_port, data_queue, stop_event):
    """
    Connects to Galaxy Buds via PyBluez RFCOMM and continuously reads head tracking data.
    """
    print(f"Attempting to connect to Galaxy Buds at {mac_address} on RFCOMM port {rfcomm_port}...")
    sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
    try:
        sock.connect((mac_address, rfcomm_port))
        print("Connected to Galaxy Buds. Sending enable command...")
        send_enable_spatial_audio_command(sock)
        sock.settimeout(0.1)

        recv_buffer = bytearray()
        EXPECTED_FULL_PACKET_LENGTH = 23

        print("Starting head tracking data reception loop.")
        while not stop_event.is_set():
            try:
                data_chunk = sock.recv(256)
                if data_chunk:
                    recv_buffer.extend(data_chunk)
                    while len(recv_buffer) >= EXPECTED_FULL_PACKET_LENGTH:
                        preamble_index = recv_buffer.find(b'\xFE')
                        if preamble_index == -1:
                            recv_buffer.clear()
                            break

                        if preamble_index > 0:
                            print(
                                f"Skipping {preamble_index} invalid byte(s) before preamble: {recv_buffer[:preamble_index].hex()}...")
                            del recv_buffer[:preamble_index]
                            if len(recv_buffer) < EXPECTED_FULL_PACKET_LENGTH:
                                break

                        packet = recv_buffer[:EXPECTED_FULL_PACKET_LENGTH]
                        quaternion = parse_galaxy_buds_head_tracking_data(packet)
                        if quaternion:
                            try:
                                if not data_queue.full():
                                    data_queue.put_nowait(quaternion)
                                else:
                                    data_queue.get_nowait()
                                    data_queue.put_nowait(quaternion)
                            except queue.Full:
                                pass

                        del recv_buffer[:EXPECTED_FULL_PACKET_LENGTH]
            except bluetooth.BluetoothError as e:
                print(f"Bluetooth error during data reception: {e}")
                stop_event.set()
                break
            except socket.timeout:
                pass
            except Exception as e:
                print(f"Error in head tracking loop: {e}")
                stop_event.set()
                break
    except bluetooth.BluetoothError as e:
        print(f"Failed to connect to Galaxy Buds: {e}")
    except Exception as e:
        if "Only one usage of each socket address" in str(e):
            print(
                "\nCritical Error: A connection attempt failed because another application is already using this Bluetooth port.")
            print(
                "Please ensure the official Galaxy Buds app or any other program using Bluetooth is completely closed.")
            print("You may need to restart your PC's Bluetooth service or reboot the computer to free the port.")
        else:
            print(f"An unexpected error occurred in the head tracking thread: {e}")
    finally:
        sock.close()
        print("Galaxy Buds head tracking connection closed.")
        stop_event.set()


# --- Audio Processing Callback ---

def audio_callback(outdata, frames, time_info, status):
    """
    Sounddevice callback function for real-time audio processing.
    """
    global current_head_orientation

    if status:
        print(f"Sounddevice status: {status}")

    try:
        while True:
            current_head_orientation = head_orientation_queue.get_nowait()
    except queue.Empty:
        pass

    chunk = audio_file.read(frames, dtype=DTYPE)
    if len(chunk) < frames:
        chunk = np.pad(chunk, ((0, frames - len(chunk)), (0, 0))) if chunk.ndim > 1 else np.pad(chunk, (
        0, frames - len(chunk)))
        if len(chunk) == 0:
            print("End of audio file or no more data, stopping stream.")
            raise sd.CallbackStop

    if chunk.ndim > 1:
        mono_chunk = chunk.mean(axis=1)
    else:
        mono_chunk = chunk

    fixed_source_vector = np.array([1.0, 0.0, 0.0])

    inverse_head_q = current_head_orientation.inverse
    rotated_source_vector = inverse_head_q.rotate(fixed_source_vector)

    azimuth_rad = np.arctan2(rotated_source_vector[1], rotated_source_vector[0])
    azimuth_deg = np.degrees(azimuth_rad)

    horizontal_distance = np.sqrt(rotated_source_vector[0] ** 2 + rotated_source_vector[1] ** 2)
    elevation_rad = np.arctan2(rotated_source_vector[2], horizontal_distance if horizontal_distance > 1e-6 else 1e-6)
    elevation_deg = np.degrees(elevation_rad)

    if azimuth_deg > 180:
        azimuth_deg -= 360
    elif azimuth_deg < -180:
        azimuth_deg += 360

    try:
        interpolated_hrir = hrtf.interpolate(azimuth_deg, elevation_deg)
        spatialized_audio = interpolated_hrir.apply(mono_chunk, samplerate=SAMPLERATE)

        if spatialized_audio.ndim == 1:
            spatialized_audio = np.stack((spatialized_audio, spatialized_audio), axis=1)

        if spatialized_audio.shape[0] < frames:
            spatialized_audio = np.pad(spatialized_audio, ((0, frames - spatialized_audio.shape[0]), (0, 0)))
        elif spatialized_audio.shape[0] > frames:
            spatialized_audio = spatialized_audio[:frames, :]

        outdata[:] = spatialized_audio
    except Exception as e:
        print(f"Error during HRTF application: {e}")
        outdata.fill(0)


# --- Main Application Logic ---

async def main():
    """
    Main function to set up the spatial audio system.
    """
    global hrtf, audio_file, current_head_orientation

    if not os.path.exists(HRTF_SOFA_FILE):
        print(f"Error: HRTF file not found at {HRTF_SOFA_FILE}. Please update the path.")
        return
    hrtf = load_hrtf(HRTF_SOFA_FILE)
    if hrtf is None:
        return

    if not os.path.exists(MUSIC_FILE_PATH):
        print(f"Error: Music file not found at {MUSIC_FILE_PATH}. Please update the path.")
        return
    audio_file = load_audio_file(MUSIC_FILE_PATH)
    if audio_file is None:
        return

    if audio_file.samplerate != SAMPLERATE:
        print(
            f"Warning: Audio file samplerate ({audio_file.samplerate}) does not match stream samplerate ({SAMPLERATE}). Consider resampling the audio file.")

    current_head_orientation = pq.Quaternion()

    discover_and_list_bluetooth_devices()

    head_tracking_thread = threading.Thread(
        target=galaxy_buds_head_tracking_thread,
        args=(GALAXY_BUDS_MAC_ADDRESS, GALAXY_BUDS_RFCOMM_PORT, head_orientation_queue, stop_event)
    )
    head_tracking_thread.daemon = True
    head_tracking_thread.start()

    print("Waiting for head tracking connection and initial data...")
    start_wait_time = time.time()
    while not stop_event.is_set() and (time.time() - start_wait_time < 15) and head_orientation_queue.empty():
        time.sleep(0.5)

    if not head_tracking_thread.is_alive() or head_orientation_queue.empty():
        print("Head tracking connection failed or no valid data received within timeout. Exiting.")
        stop_event.set()
        if audio_file:
            audio_file.close()
        return

    print("Starting audio stream...")
    try:
        with sd.OutputStream(
                samplerate=SAMPLERATE,
                blocksize=BLOCKSIZE,
                channels=CHANNELS,
                dtype=DTYPE,
                callback=audio_callback
        ) as stream:
            print(f"Audio stream started. Playing '{os.path.basename(MUSIC_FILE_PATH)}' with spatial audio.")
            print("Press Ctrl+C to stop.")
            while stream.active and not stop_event.is_set():
                time.sleep(0.1)
    except Exception as e:
        print(f"An error occurred with the audio stream: {e}")
    finally:
        print("Stopping services...")
        stop_event.set()
        if head_tracking_thread.is_alive():
            head_tracking_thread.join(timeout=5)
        if audio_file:
            audio_file.close()
        print("Application stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nUser interrupted, stopping.")
        stop_event.set()
    except Exception as e:
        print(f"An unhandled error occurred: {e}")
