import os, sys
import shutil
import gc
import torch
from multiprocessing import cpu_count
from rvc_inferpy.modules import VC
from rvc_inferpy.split_audio import (
    split_silence_nonsilent,
    adjust_audio_lengths,
    combine_silence_nonsilent,
)
from pathlib import Path
import requests
import os
import zipfile
import shutil
import urllib.request
import gdown

models_dir = "models"


class Configs:
    def __init__(self, device, is_half):
        self.device = device
        self.is_half = is_half
        self.n_cpu = 0
        self.gpu_name = None
        self.gpu_mem = None
        self.x_pad, self.x_query, self.x_center, self.x_max = self.device_config()

    def device_config(self) -> tuple:
        if torch.cuda.is_available():
            i_device = int(self.device.split(":")[-1])
            self.gpu_name = torch.cuda.get_device_name(i_device)
        elif torch.backends.mps.is_available():
            print("No supported N-card found, use MPS for inference")
            self.device = "mps"
        else:
            print("No supported N-card found, use CPU for inference")
            self.device = "cpu"

        if self.n_cpu == 0:
            self.n_cpu = cpu_count()

        if self.is_half:
            # 6G memory config
            x_pad = 3
            x_query = 10
            x_center = 60
            x_max = 65
        else:
            # 5G memory config
            x_pad = 1
            x_query = 6
            x_center = 38
            x_max = 41

        if self.gpu_mem != None and self.gpu_mem <= 4:
            x_pad = 1
            x_query = 5
            x_center = 30
            x_max = 32

        return x_pad, x_query, x_center, x_max


def get_model(voice_model):
    model_dir = os.path.join(os.getcwd(), "models", voice_model)
    model_filename, index_filename = None, None
    for file in os.listdir(model_dir):
        ext = os.path.splitext(file)[1]
        if ext == ".pth":
            model_filename = file
        if ext == ".index":
            index_filename = file

    if model_filename is None:
        print(f"No model file exists in {models_dir}.")
        return None, None

    return os.path.join(model_dir, model_filename), (
        os.path.join(model_dir, index_filename) if index_filename else ""
    )


BASE_DIR = Path(os.getcwd())
sys.path.append(str(BASE_DIR))

files_to_check = ["hubert_base.pt", "rmvpe.pt", "fcpe.pt"]

missing_files = [file for file in files_to_check if not (BASE_DIR / file).exists()]


def dl_model(link, model_name, dir_name):
    url = f"{link}/{model_name}"
    response = requests.get(url, stream=True)
    response.raise_for_status()

    target_path = dir_name / model_name
    target_path.parent.mkdir(
        parents=True, exist_ok=True
    )  # Create the directory if it doesn't exist

    with open(target_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

    print(f"{model_name} downloaded successfully!")


if missing_files:
    RVC_DOWNLOAD_LINK = "https://huggingface.co/theNeofr/rvc-base/resolve/main"  # Replace with the actual download link

    for model in missing_files:
        print(f"Downloading {model}...")
        dl_model(RVC_DOWNLOAD_LINK, model, BASE_DIR)

    print("All missing models have been downloaded!")
else:
    pass



def extract_zip(extraction_folder, zip_name):
    os.makedirs(extraction_folder, exist_ok=True)
    with zipfile.ZipFile(zip_name, 'r') as zip_ref:
        zip_ref.extractall(extraction_folder)
    os.remove(zip_name)

    index_filepath, model_filepath = None, None
    for root, dirs, files in os.walk(extraction_folder):
        for name in files:
            if name.endswith('.index') and os.stat(os.path.join(root, name)).st_size > 1024 * 100:
                index_filepath = os.path.join(root, name)

            if name.endswith('.pth') and os.stat(os.path.join(root, name)).st_size > 1024 * 1024 * 40:
                model_filepath = os.path.join(root, name)

    if not model_filepath:
        raise Exception(f'No .pth model file was found in the extracted zip. Please check {extraction_folder}.')

    os.rename(model_filepath, os.path.join(extraction_folder, os.path.basename(model_filepath)))
    if index_filepath:
        os.rename(index_filepath, os.path.join(extraction_folder, os.path.basename(index_filepath)))

    # Remove unnecessary nested folders
    for filepath in os.listdir(extraction_folder):
        if os.path.isdir(os.path.join(extraction_folder, filepath)):
            shutil.rmtree(os.path.join(extraction_folder, filepath))

def download_rvc_model(url, dir_name):
    try:
        print(f'[~] Downloading voice model with name {dir_name}...')
        zip_name = url.split('/')[-1]
        extraction_folder = os.path.join(models_dir, dir_name)
        if os.path.exists(extraction_folder):
            raise Exception(f'Voice model directory {dir_name} already exists! Choose a different name for your voice model.')

        if 'pixeldrain.com' in url:
            url = f'https://pixeldrain.com/api/file/{zip_name}'
        if 'drive.google.com' in url:
            zip_name = dir_name + ".zip"
            gdown.download(url, output=zip_name, use_cookies=True, quiet=True, fuzzy=True)
        else:
            urllib.request.urlretrieve(url, zip_name)

        print(f'[~] Extracting zip file...')
        extract_zip(extraction_folder, zip_name)
        print(f'[+] {dir_name} Model successfully downloaded!')

    except Exception as e:
        raise Exception(str(e))


def infer_audio(
    model_name,
    audio_path,
    f0_change=0,
    f0_method="rmvpe+",
    min_pitch="50",
    max_pitch="1100",
    crepe_hop_length=128,
    index_rate=0.75,
    filter_radius=3,
    rms_mix_rate=0.25,
    protect=0.33,
    split_infer=False,
    min_silence=500,
    silence_threshold=-50,
    seek_step=1,
    keep_silence=100,
    do_formant=False,
    quefrency=0,
    timbre=1,
    f0_autotune=False,
    audio_format="wav",
    resample_sr=0,
    hubert_model_path="hubert_base.pt",
    rmvpe_model_path="rmvpe.pt",
    fcpe_model_path="fcpe.pt",
):
    os.environ["rmvpe_model_path"] = rmvpe_model_path
    os.environ["fcpe_model_path"] = fcpe_model_path
    configs = Configs("cuda:0", True)
    vc = VC(configs)
    pth_path, index_path = get_model(model_name)
    vc_data = vc.get_vc(pth_path, protect, 0.5)

    if split_infer:
        inferred_files = []
        temp_dir = os.path.join(os.getcwd(), "seperate", "temp")
        os.makedirs(temp_dir, exist_ok=True)
        print("Splitting audio to silence and nonsilent segments.")
        silence_files, nonsilent_files = split_silence_nonsilent(
            audio_path, min_silence, silence_threshold, seek_step, keep_silence
        )
        print(
            f"Total silence segments: {len(silence_files)}.\nTotal nonsilent segments: {len(nonsilent_files)}."
        )
        for i, nonsilent_file in enumerate(nonsilent_files):
            print(f"Inferring nonsilent audio {i+1}")
            inference_info, audio_data, output_path = vc.vc_single(
                0,
                nonsilent_file,
                f0_change,
                f0_method,
                index_path,
                index_path,
                index_rate,
                filter_radius,
                resample_sr,
                rms_mix_rate,
                protect,
                audio_format,
                crepe_hop_length,
                do_formant,
                quefrency,
                timbre,
                min_pitch,
                max_pitch,
                f0_autotune,
                hubert_model_path,
            )
            if inference_info[0] == "Success.":
                print("Inference ran successfully.")
                print(inference_info[1])
                print(
                    "Times:\nnpy: %.2fs f0: %.2fs infer: %.2fs\nTotal time: %.2fs"
                    % (*inference_info[2],)
                )
            else:
                print(f"An error occurred while processing.\n{inference_info[0]}")
                return None
            inferred_files.append(output_path)
        print("Adjusting inferred audio lengths.")
        adjusted_inferred_files = adjust_audio_lengths(nonsilent_files, inferred_files)
        print("Combining silence and inferred audios.")
        output_count = 1
        while True:
            output_path = os.path.join(
                os.getcwd(),
                "output",
                f"{os.path.splitext(os.path.basename(audio_path))[0]}{model_name}{f0_method.capitalize()}_{output_count}.{audio_format}",
            )
            if not os.path.exists(output_path):
                break
            output_count += 1
        output_path = combine_silence_nonsilent(
            silence_files, adjusted_inferred_files, keep_silence, output_path
        )
        [shutil.move(inferred_file, temp_dir) for inferred_file in inferred_files]
        shutil.rmtree(temp_dir)
    else:
        inference_info, audio_data, output_path = vc.vc_single(
            0,
            audio_path,
            f0_change,
            f0_method,
            index_path,
            index_path,
            index_rate,
            filter_radius,
            resample_sr,
            rms_mix_rate,
            protect,
            audio_format,
            crepe_hop_length,
            do_formant,
            quefrency,
            timbre,
            min_pitch,
            max_pitch,
            f0_autotune,
            hubert_model_path,
        )
        if inference_info[0] == "Success.":
            print("Inference ran successfully.")
            print(inference_info[1])
            print(
                "Times:\nnpy: %.2fs f0: %.2fs infer: %.2fs\nTotal time: %.2fs"
                % (*inference_info[2],)
            )
        else:
            print(f"An error occurred while processing.\n{inference_info[0]}")
            del configs, vc
            gc.collect()
            return inference_info[0]

    del configs, vc
    gc.collect()
    return output_path
