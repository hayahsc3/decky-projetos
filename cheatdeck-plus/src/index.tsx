import {
  ButtonItem,
  DropdownItem,
  PanelSection,
  PanelSectionRow,
  Router,
  SliderField,
  TextField,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { definePlugin, toaster } from "@decky/api";
import { useCallback, useEffect, useState } from "react";
import type { ChangeEvent, ReactNode } from "react";
import { FaGamepad } from "react-icons/fa";

import * as api from "./api";
import type {
  DownloadItem,
  FlingFile,
  FlingResult,
  LaunchMode,
  Result,
  Runtime,
  Settings,
  TrainerItem,
} from "./api";

const DEFAULT_SETTINGS: Settings = { launch_mode: "auto", launch_delay: 0, auto_close: true };

const MODE_OPTIONS: { label: string; data: LaunchMode }[] = [
  { label: "Automático (container, depois direto)", data: "auto" },
  { label: "Somente container", data: "container" },
  { label: "Somente direto (proton run)", data: "direct" },
];

const POLL_MS = 5000;

const fmtSize = (bytes: number): string => {
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
};

// Appid do jogo em foco (atalhos não-Steam também aparecem aqui).
const readRunningAppId = (): string => {
  const id = Router.MainRunningApp?.appid;
  return id ? String(id) : "";
};

function useRunningAppId(): string {
  const [appId, setAppId] = useState<string>(readRunningAppId());
  useEffect(() => {
    const t = setInterval(() => setAppId(readRunningAppId()), 3000);
    return () => clearInterval(t);
  }, []);
  return appId;
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <PanelSection title={title}>
      {children}
    </PanelSection>
  );
}

function Content() {
  const appId = useRunningAppId();

  const [runtime, setRuntime] = useState<Runtime | null>(null);
  const [trainers, setTrainers] = useState<TrainerItem[]>([]);
  const [downloads, setDownloads] = useState<DownloadItem[]>([]);
  const [settings, setSettings] = useState<Settings>(DEFAULT_SETTINGS);
  const [busy, setBusy] = useState(false);

  const [query, setQuery] = useState("");
  const [results, setResults] = useState<FlingResult[]>([]);
  const [files, setFiles] = useState<FlingFile[]>([]);
  const [filesTitle, setFilesTitle] = useState("");
  const [directUrl, setDirectUrl] = useState("");
  const [diag, setDiag] = useState("");

  const refresh = useCallback(async () => {
    if (!appId) {
      setRuntime(null);
      setTrainers([]);
      return;
    }
    try {
      const [rt, tr] = await Promise.all([api.getRuntime(appId), api.listTrainers(appId)]);
      setRuntime(rt);
      setTrainers(tr);
    } catch (e) {
      console.error("CheatDeck Plus refresh:", e);
    }
  }, [appId]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, POLL_MS);
    return () => clearInterval(t);
  }, [refresh]);

  useEffect(() => {
    api.getSettings().then(setSettings).catch(() => undefined);
    api.listDownloads().then(setDownloads).catch(() => undefined);
  }, []);

  const notify = (title: string, r: Result) =>
    toaster.toast({ title, body: r.message });

  // Executa uma ação do backend, notifica e atualiza a lista.
  const run = async (title: string, action: () => Promise<Result>) => {
    setBusy(true);
    try {
      notify(title, await action());
    } catch (e) {
      toaster.toast({ title, body: `Erro: ${String(e)}` });
    } finally {
      setBusy(false);
      refresh();
    }
  };

  const requireGame = (): string | null => {
    if (!appId) {
      toaster.toast({ title: "CheatDeck Plus", body: "Abra um jogo primeiro." });
      return null;
    }
    return appId;
  };

  const doSearch = async () => {
    if (!query.trim()) return;
    setBusy(true);
    setFiles([]);
    try {
      const r = await api.flingSearch(query);
      setResults(r.results);
      if (!r.ok) toaster.toast({ title: "FLiNG", body: r.message ?? "Busca falhou." });
      else if (r.results.length === 0) toaster.toast({ title: "FLiNG", body: "Nenhum resultado." });
    } catch (e) {
      toaster.toast({ title: "FLiNG", body: `Erro: ${String(e)}` });
    } finally {
      setBusy(false);
    }
  };

  const openResult = async (r: FlingResult) => {
    setBusy(true);
    try {
      const res = await api.flingFiles(r.url);
      setFilesTitle(r.title);
      setFiles(res.files);
      if (!res.ok) toaster.toast({ title: "FLiNG", body: res.message ?? "Falha ao ler a página." });
    } catch (e) {
      toaster.toast({ title: "FLiNG", body: `Erro: ${String(e)}` });
    } finally {
      setBusy(false);
    }
  };

  const changeSetting = async (patch: Partial<Settings>) => {
    try {
      setSettings(await api.setSettings(patch));
    } catch (e) {
      toaster.toast({ title: "Configurações", body: `Erro: ${String(e)}` });
    }
  };

  const showDiagnose = async () => {
    setBusy(true);
    try {
      setDiag(await api.diagnose(appId || "0"));
    } finally {
      setBusy(false);
    }
  };

  const gameRunning = runtime?.running ?? false;
  const trainerRunning = runtime?.trainer_running ?? false;
  const selectedMode = MODE_OPTIONS.find((o) => o.data === settings.launch_mode) ?? MODE_OPTIONS[0];

  return (
    <>
      <Section title="Jogo">
        <PanelSectionRow>
          <div className={staticClasses.Text}>
            {!appId
              ? "Nenhum jogo em foco."
              : gameRunning
                ? `Jogo detectado (appid ${appId}). Prefixo Proton pronto.`
                : `appid ${appId}: jogo ainda não está rodando. Abra o jogo para liberar o trainer.`}
          </div>
        </PanelSectionRow>
        {trainerRunning && (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              disabled={busy}
              onClick={() => appId && run("Parar trainer", () => api.stopTrainer(appId))}
            >
              Parar trainer em execução
            </ButtonItem>
          </PanelSectionRow>
        )}
      </Section>

      <Section title="Trainers instalados">
        {!appId && (
          <PanelSectionRow>
            <div className={staticClasses.Text}>Os trainers são guardados por jogo.</div>
          </PanelSectionRow>
        )}
        {appId && trainers.length === 0 && (
          <PanelSectionRow>
            <div className={staticClasses.Text}>Nenhum trainer para este jogo ainda.</div>
          </PanelSectionRow>
        )}
        {trainers.map((t) => (
          <PanelSectionRow key={t.rel}>
            <ButtonItem
              layout="below"
              disabled={busy || !gameRunning || trainerRunning}
              onClick={() => appId && run(`Abrir ${t.name}`, () => api.launchTrainer(appId, t.rel))}
            >
              {`▶ ${t.group} (${fmtSize(t.size)})`}
            </ButtonItem>
          </PanelSectionRow>
        ))}
        {appId && trainers.length > 0 && (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              disabled={busy}
              onClick={() => {
                const last = trainers[0];
                if (last) run(`Remover ${last.group}`, () => api.deleteTrainer(appId, last.rel));
              }}
            >
              Remover o trainer mais recente
            </ButtonItem>
          </PanelSectionRow>
        )}
      </Section>

      <Section title="Buscar no FLiNG">
        <PanelSectionRow>
          <TextField
            label="Nome do jogo"
            value={query}
            onChange={(e: ChangeEvent<HTMLInputElement>) => setQuery(e.target.value)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy || !query.trim()} onClick={doSearch}>
            Buscar
          </ButtonItem>
        </PanelSectionRow>
        {results.map((r) => (
          <PanelSectionRow key={r.url}>
            <ButtonItem layout="below" disabled={busy} onClick={() => openResult(r)}>
              {r.title}
            </ButtonItem>
          </PanelSectionRow>
        ))}
        {files.length > 0 && (
          <PanelSectionRow>
            <div className={staticClasses.Text}>{`Arquivos de: ${filesTitle}`}</div>
          </PanelSectionRow>
        )}
        {files.map((f) => (
          <PanelSectionRow key={f.url}>
            <ButtonItem
              layout="below"
              disabled={busy || !appId}
              onClick={() => {
                const ok = requireGame();
                if (ok) run(`Baixar ${f.label}`, () => api.downloadUrl(ok, f.url));
              }}
            >
              {`⬇ ${f.label}`}
            </ButtonItem>
          </PanelSectionRow>
        ))}
      </Section>

      <Section title="Baixar por link">
        <PanelSectionRow>
          <TextField
            label="Link direto (.exe, .zip, .rar, .7z)"
            value={directUrl}
            onChange={(e: ChangeEvent<HTMLInputElement>) => setDirectUrl(e.target.value)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy || !directUrl.trim() || !appId}
            onClick={() => {
              const ok = requireGame();
              if (ok) run("Baixar link", () => api.downloadUrl(ok, directUrl.trim()));
            }}
          >
            Baixar e importar
          </ButtonItem>
        </PanelSectionRow>
      </Section>

      <Section title="Importar de Downloads / Desktop">
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={() => api.listDownloads().then(setDownloads)}>
            Atualizar lista
          </ButtonItem>
        </PanelSectionRow>
        {downloads.length === 0 && (
          <PanelSectionRow>
            <div className={staticClasses.Text}>Nenhum .exe ou arquivo compactado encontrado.</div>
          </PanelSectionRow>
        )}
        {downloads.map((d) => (
          <PanelSectionRow key={d.path}>
            <ButtonItem
              layout="below"
              disabled={busy || !appId}
              onClick={() => {
                const ok = requireGame();
                if (ok) run(`Importar ${d.name}`, () => api.importDownload(ok, d.path));
              }}
            >
              {`${d.likely ? "★ " : ""}${d.name} (${fmtSize(d.size)})`}
            </ButtonItem>
          </PanelSectionRow>
        ))}
      </Section>

      <Section title="Configurações">
        <PanelSectionRow>
          <DropdownItem
            label="Modo de lançamento"
            rgOptions={MODE_OPTIONS}
            selectedOption={selectedMode.data}
            onChange={(o: { data: LaunchMode }) => changeSetting({ launch_mode: o.data })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <SliderField
            label="Atraso antes de abrir (s)"
            value={settings.launch_delay}
            min={0}
            max={60}
            step={1}
            showValue
            onChange={(v: number) => changeSetting({ launch_delay: v })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Fechar trainer quando o jogo fechar"
            checked={settings.auto_close}
            onChange={(v: boolean) => changeSetting({ auto_close: v })}
          />
        </PanelSectionRow>
      </Section>

      <Section title="Suporte">
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={showDiagnose}>
            Diagnóstico
          </ButtonItem>
        </PanelSectionRow>
        {diag && (
          <PanelSectionRow>
            <div style={{ whiteSpace: "pre-wrap", fontSize: 11, fontFamily: "monospace" }}>{diag}</div>
          </PanelSectionRow>
        )}
      </Section>
    </>
  );
}

export default definePlugin(() => {
  return {
    title: <div className={staticClasses.Title}>CheatDeck Plus</div>,
    content: <Content />,
    icon: <FaGamepad />,
    onDismount() {
      // Nada a limpar: os timers ficam dentro do componente.
    },
  };
});
