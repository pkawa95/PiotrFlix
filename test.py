import subprocess

def map_network_drive():
    subprocess.run(
        ['net', 'use', 'Z:', r'\\MYCLOUD-00A2RY\kawjorek', '/persistent:no'],
        shell=True
    )

map_network_drive()