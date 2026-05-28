"""
Test script to play 440Hz tone on each ALSA channel individually
"""
import pyaudio
import numpy as np
import time

# Audio parameters
SAMPLE_RATE = 48000
DURATION = 2  # seconds per channel
FREQUENCY = 440  # Hz (A4 note)
NUM_CHANNELS = 8

# Generate tone
def generate_tone(duration, frequency, sample_rate):
    """Generate a sine wave tone"""
    samples = int(duration * sample_rate)
    t = np.linspace(0, duration, samples, False)
    tone = np.sin(2 * np.pi * frequency * t) * 0.3  # 30% volume
    return tone.astype(np.float32)

def main():
    p = pyaudio.PyAudio()
    
    # Generate the tone once
    tone = generate_tone(DURATION, FREQUENCY, SAMPLE_RATE)
    
    print("=" * 60)
    print("ALSA Channel Test - RaspiAudio I2S HAT")
    print("=" * 60)
    print(f"Playing {FREQUENCY}Hz tone on each channel for {DURATION} seconds")
    print("Listen carefully to identify which physical jack each channel goes to")
    print("=" * 60)
    
    # Test each channel
    for channel in range(NUM_CHANNELS):
        print(f"\n>>> Testing ALSA Channel {channel} <<<")
        print(f"Listen on all jacks to see where you hear this tone...")
        
        # Create multi-channel audio with tone only on the target channel
        multi_channel_audio = np.zeros((len(tone), NUM_CHANNELS), dtype=np.float32)
        multi_channel_audio[:, channel] = tone
        
        # Open stream
        stream = p.open(
            format=pyaudio.paFloat32,
            channels=NUM_CHANNELS,
            rate=SAMPLE_RATE,
            output=True,
            output_device_index=None,  # Will use default (hw:0,0)
        )
        
        # Play the tone
        stream.write(multi_channel_audio.tobytes())
        stream.stop_stream()
        stream.close()
        
        # Pause between channels
        time.sleep(1)
    
    print("\n" + "=" * 60)
    print("Test complete!")
    print("=" * 60)
    
    p.terminate()

if __name__ == "__main__":
    main()
