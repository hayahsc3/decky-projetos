import { callable } from "@decky/api";

// Tipos espelhando os retornos do main.py (backend Python).

export type LaunchMode = "auto" | "container" | "direct";

export interface Runtime {
  running: boolean;
  proton: string;
  prefix: string;
  trainer_running: boolean;
}

export interface TrainerItem {
  rel: string;
  name: string;
  group: string;
  size: number;
  mtime: number;
}

export interface DownloadItem {
  path: string;
  name: string;
  size: number;
  mtime: number;
  likely: boolean;
}

export interface FlingResult {
  url: string;
  title: string;
}

export interface FlingFile {
  url: string;
  label: string;
}

export interface Result {
  ok: boolean;
  message: string;
}

export interface Settings {
  launch_mode: LaunchMode;
  launch_delay: number;
  auto_close: boolean;
}

export const getRuntime = callable<[appid: string], Runtime>("get_runtime");
export const listTrainers = callable<[appid: string], TrainerItem[]>("list_trainers");
export const listDownloads = callable<[], DownloadItem[]>("list_downloads");
export const importDownload = callable<[appid: string, path: string], Result>("import_download");
export const downloadUrl = callable<[appid: string, url: string], Result>("download_url");
export const deleteTrainer = callable<[appid: string, rel: string], Result>("delete_trainer");
export const launchTrainer = callable<[appid: string, rel: string], Result>("launch_trainer");
export const stopTrainer = callable<[appid: string], Result>("stop_trainer");
export const flingSearch = callable<
  [query: string],
  { ok: boolean; results: FlingResult[]; message?: string }
>("fling_search");
export const flingFiles = callable<
  [pageUrl: string],
  { ok: boolean; files: FlingFile[]; message?: string }
>("fling_files");
export const diagnose = callable<[appid: string], string>("diagnose");
export const getSettings = callable<[], Settings>("get_settings");
export const setSettings = callable<[settings: Partial<Settings>], Settings>("set_settings");
