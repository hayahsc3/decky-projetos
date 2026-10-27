import {
  ButtonItem,
  ConfirmModal,
  DialogBody,
  DialogButton,
  DialogHeader,
  Field,
  Focusable,
  ModalRoot,
  PanelSection,
  PanelSectionRow,
  SliderField,
  TextField,
  ToggleField,
  showModal,
  staticClasses,
} from "@decky/ui";
import {
  addEventListener,
  callable,
  definePlugin,
  removeEventListener,
  toaster,
} from "@decky/api";
import { FC, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { FaSdCard } from "react-icons/fa";

// ---------------------------------------------------------------- tipos

interface SdInfo {
  mount: string;
  device: string;
  fs: string;
  total: number;
  free: number;
  writable: boolean;
  label: string;
  custom: boolean;
}

interface Game {
  appid: number;
  name: string;
  kind: "steam" | "nonsteam";
  has_data: boolean;
  backup_count: number;
  last_backup: number | null;
  backup_size: number;
}

interface Orphan {
  appid: number;
  name: string;
  backup_count: number;
  last_backup: number | null;
}

interface ScanResult {
  sd: SdInfo | null;
  sd_error: string;
  steam_found: boolean;
  games: Game[];
  orphans: Orphan[];
  settings: { max_backups: number; destination: string; auto_backup: boolean; auto_mount: boolean };
}

interface Backup {
  filename: string;
  size: number;
  ts: number;
  pre_restore: boolean;
}

interface UpdateStatus {
  current: string;
  latest: string;
  available: boolean;
  url: string;
  error: string;
  has_token: boolean;
  notified: boolean;
}

interface OpResult {
  ok: boolean;
  message: string;
}

// ------------------------------------------------------- backend (main.py)

const scanCall = callable<[], ScanResult>("scan");
const listBackupsCall = callable<[appid: number], Backup[]>("list_backups");
const backupGameCall = callable<[appid: number], OpResult>("backup_game");
const backupAllCall = callable<[], OpResult>("backup_all");
const restoreGameCall = callable<[appid: number, filename: string], OpResult>("restore_game");
const deleteBackupCall = callable<[appid: number, filename: string], OpResult>("delete_backup");
const setAutoBackupCall = callable<[enabled: boolean], { auto_backup: boolean }>("set_auto_backup");
const updateStatusCall = callable<[force: boolean], UpdateStatus>("get_update_status");
const ackUpdateCall = callable<[version: string], boolean>("ack_update");
const setTokenCall = callable<[token: string], boolean>("set_github_token");
const mountSdCall = callable<[], OpResult>("mount_sd");
const setAutoMountCall = callable<[enabled: boolean], { auto_mount: boolean }>("set_auto_mount");
const setMaxBackupsCall = callable<[value: number], { max_backups: number }>("set_max_backups");

// ---------------------------------------------------------------- helpers

const fmtSize = (bytes: number): string => {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = bytes;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
};

const fmtDate = (ts: number | null): string =>
  ts
    ? new Date(ts * 1000).toLocaleString("pt-BR", {
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";

const describeGame = (g: Game): string => {
  if (g.backup_count > 0) {
    return `${g.backup_count} backup(s) · último: ${fmtDate(g.last_backup)}`;
  }
  return g.has_data ? "Sem backup no microSD" : "Nunca executado (sem saves ainda)";
};

const errText = (e: unknown): string => (e instanceof Error ? e.message : String(e));

// ------------------------------------------------------------ modal do jogo

interface GameModalProps {
  game: Game;
  sdOk: boolean;
  onChanged: () => void;
  closeModal?: () => void;
}

const GameModal: FC<GameModalProps> = ({ game, sdOk, onChanged, closeModal }) => {
  const [backups, setBackups] = useState<Backup[]>([]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  const load = useCallback(async () => {
    try {
      setBackups(await listBackupsCall(game.appid));
    } catch (e) {
      setMsg(`Erro ao listar backups: ${errText(e)}`);
    }
  }, [game.appid]);

  useEffect(() => {
    void load();
  }, [load]);

  const run = async (fn: () => Promise<OpResult>) => {
    setBusy(true);
    setMsg("");
    try {
      const r = await fn();
      setMsg(r.message);
      toaster.toast({ title: game.name, body: r.message });
      await load();
      onChanged();
    } catch (e) {
      setMsg(`Erro: ${errText(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const askRestore = (b: Backup) => {
    showModal(
      <ConfirmModal
        strTitle="Restaurar save?"
        strDescription={
          `Os saves atuais de "${game.name}" serão substituídos pelo backup de ${fmtDate(b.ts)}. ` +
          "Um backup de segurança do estado atual é criado antes. Feche o jogo antes de continuar. " +
          "Se o jogo usa Steam Cloud, a Steam pode sincronizar de novo ao abri-lo."
        }
        strOKButtonText="Restaurar"
        strCancelButtonText="Cancelar"
        onOK={() => void run(() => restoreGameCall(game.appid, b.filename))}
      />
    );
  };

  const askDelete = (b: Backup) => {
    showModal(
      <ConfirmModal
        strTitle="Excluir backup?"
        strDescription={`Excluir o backup de ${fmtDate(b.ts)} do microSD? Isso não pode ser desfeito.`}
        strOKButtonText="Excluir"
        strCancelButtonText="Cancelar"
        onOK={() => void run(() => deleteBackupCall(game.appid, b.filename))}
      />
    );
  };

  return (
    <ModalRoot closeModal={closeModal}>
      <DialogHeader>{game.name}</DialogHeader>
      <DialogBody>
        <Field
          label="Backup"
          description={
            sdOk
              ? "Copia os saves deste jogo para o microSD."
              : "microSD indisponível. Insira o cartão e atualize a lista."
          }
        >
          <DialogButton
            disabled={busy || !sdOk}
            onClick={() => void run(() => backupGameCall(game.appid))}
          >
            {busy ? "Aguarde…" : "Fazer backup agora"}
          </DialogButton>
        </Field>

        {msg && <Field label="Resultado" description={msg} />}

        <Field label={`Backups no microSD (${backups.length})`} />
        {backups.length === 0 && (
          <Field description="Nenhum backup deste jogo no microSD." />
        )}
        {backups.map((b) => (
          <Field
            key={b.filename}
            label={fmtDate(b.ts)}
            description={`${fmtSize(b.size)}${b.pre_restore ? " · segurança (antes de restaurar)" : ""}`}
          >
            <Focusable style={{ display: "flex", gap: "8px" }}>
              <DialogButton disabled={busy} onClick={() => askRestore(b)}>
                Restaurar
              </DialogButton>
              <DialogButton disabled={busy} onClick={() => askDelete(b)}>
                Excluir
              </DialogButton>
            </Focusable>
          </Field>
        ))}
      </DialogBody>
    </ModalRoot>
  );
};

// ------------------------------------------------------------- painel

const PAGE_SIZE = 60;

const Content: FC = () => {
  const [data, setData] = useState<ScanResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");
  const [showAll, setShowAll] = useState(false);
  const [busyAll, setBusyAll] = useState(false);
  const [progress, setProgress] = useState("");
  const [maxBackups, setMaxBackups] = useState(5);
  const [autoBackup, setAutoBackup] = useState(false);
  const [autoMount, setAutoMount] = useState(true);
  const [mounting, setMounting] = useState(false);
  const [upd, setUpd] = useState<UpdateStatus | null>(null);
  const [checking, setChecking] = useState(false);
  const [token, setToken] = useState("");
  const saveTimer = useRef<number | undefined>(undefined);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const r = await scanCall();
      setData(r);
      setMaxBackups(r.settings.max_backups);
      setAutoBackup(r.settings.auto_backup);
      setAutoMount(r.settings.auto_mount);
    } catch (e) {
      setError(errText(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    updateStatusCall(false).then(setUpd).catch(() => undefined);
  }, []);

  const checkUpdates = async () => {
    setChecking(true);
    try {
      const r = await updateStatusCall(true);
      setUpd(r);
      toaster.toast({
        title: "SaveBackupSD",
        body: r.error
          ? r.error
          : r.available
            ? `Atualização disponível: v${r.current} → v${r.latest}`
            : `Você está na versão mais recente (v${r.current}).`,
      });
    } catch (e) {
      toaster.toast({ title: "SaveBackupSD", body: `Erro: ${errText(e)}` });
    } finally {
      setChecking(false);
    }
  };

  const saveToken = async () => {
    try {
      await setTokenCall(token);
      setToken("");
      await checkUpdates();
    } catch (e) {
      toaster.toast({ title: "SaveBackupSD", body: `Erro: ${errText(e)}` });
    }
  };

  useEffect(() => {
    const listener = addEventListener<[done: number, total: number, name: string]>(
      "backup_progress",
      (done, total, name) => setProgress(`${done}/${total} — ${name}`)
    );
    return () => removeEventListener("backup_progress", listener);
  }, []);

  useEffect(
    () => () => {
      if (saveTimer.current !== undefined) window.clearTimeout(saveTimer.current);
    },
    []
  );

  const sdOk = !!data?.sd && data.sd.writable;

  const backupAll = async () => {
    setBusyAll(true);
    setProgress("");
    try {
      const r = await backupAllCall();
      toaster.toast({ title: "Backup de todos os jogos", body: r.message });
    } catch (e) {
      toaster.toast({ title: "Backup de todos os jogos", body: `Erro: ${errText(e)}` });
    } finally {
      setBusyAll(false);
      setProgress("");
      void refresh();
    }
  };

  const mountNow = async () => {
    setMounting(true);
    try {
      const r = await mountSdCall();
      toaster.toast({ title: "microSD", body: r.message });
    } catch (e) {
      toaster.toast({ title: "microSD", body: `Erro: ${errText(e)}` });
    } finally {
      setMounting(false);
      void refresh();
    }
  };

  const onAutoMount = (v: boolean) => {
    setAutoMount(v);
    setAutoMountCall(v).catch((e: unknown) => {
      setAutoMount(!v);
      toaster.toast({ title: "Montagem automática", body: `Erro: ${errText(e)}` });
    });
  };

  const onAutoBackup = (v: boolean) => {
    setAutoBackup(v);
    setAutoBackupCall(v).catch((e: unknown) => {
      setAutoBackup(!v);
      toaster.toast({ title: "Backup automático", body: `Erro: ${errText(e)}` });
    });
  };

  const onSlider = (v: number) => {
    setMaxBackups(v);
    if (saveTimer.current !== undefined) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      void setMaxBackupsCall(v);
    }, 500);
  };

  const filtered = useMemo(() => {
    const games = data?.games ?? [];
    const q = filter.trim().toLowerCase();
    return q ? games.filter((g) => g.name.toLowerCase().includes(q)) : games;
  }, [data, filter]);

  const visible = showAll || filter.trim() ? filtered : filtered.slice(0, PAGE_SIZE);

  const openGame = (g: Game) => {
    showModal(<GameModal game={g} sdOk={sdOk} onChanged={() => void refresh()} />);
  };

  return (
    <>
      <PanelSection title="Versão">
        <PanelSectionRow>
          <Field
            label={`SaveBackupSD v${upd?.current ?? "…"}`}
            description={
              !upd
                ? "Verificando atualizações…"
                : upd.error
                  ? upd.error
                  : upd.available
                    ? `Atualização disponível: v${upd.latest}. Baixe o zip em Releases no GitHub e instale pelo Decky.`
                    : "Você está na versão mais recente."
            }
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={checking} onClick={() => void checkUpdates()}>
            {checking ? "Verificando…" : "Verificar atualizações"}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="MicroSD">
        <PanelSectionRow>
          {data?.sd ? (
            <Field
              label={`${data.sd.label}${data.sd.custom ? " (destino personalizado)" : ""}`}
              description={
                `${fmtSize(data.sd.free)} livres de ${fmtSize(data.sd.total)}` +
                (data.sd.writable ? "" : " · somente leitura")
              }
            />
          ) : (
            <Field
              label="Cartão não encontrado"
              description={data?.sd_error || (loading ? "Procurando…" : "Insira o microSD.")}
            />
          )}
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={loading || busyAll || mounting} onClick={() => void refresh()}>
            {loading ? "Atualizando…" : "Atualizar"}
          </ButtonItem>
        </PanelSectionRow>
        {data && !data.sd && (
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={mounting || loading} onClick={() => void mountNow()}>
              {mounting ? "Montando…" : "Montar microSD"}
            </ButtonItem>
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ToggleField
            label="Montar microSD automaticamente"
            description="Se o cartão estiver inserido mas desmontado, o plugin monta sozinho"
            checked={autoMount}
            onChange={onAutoMount}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={!sdOk || busyAll || loading}
            onClick={() => void backupAll()}
            description={busyAll ? progress || "Iniciando…" : "Salva todos os jogos que já têm saves"}
          >
            {busyAll ? "Fazendo backup…" : "Backup de todos os jogos"}
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Backup automático"
            description="Salva os saves assim que você fecha um jogo (só se algo mudou)"
            checked={autoBackup}
            onChange={onAutoBackup}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <SliderField
            label="Backups por jogo"
            description="Os mais antigos são removidos automaticamente"
            value={maxBackups}
            min={1}
            max={20}
            step={1}
            showValue
            onChange={onSlider}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title={`Jogos instalados${data ? ` (${data.games.length})` : ""}`}>
        {error && (
          <PanelSectionRow>
            <Field label="Erro" description={error} />
          </PanelSectionRow>
        )}
        {data && !data.steam_found && (
          <PanelSectionRow>
            <Field label="Steam não encontrada" description="Não achei a pasta da Steam no usuário atual." />
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <TextField
            label="Buscar jogo"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        </PanelSectionRow>
        {visible.map((g) => (
          <PanelSectionRow key={g.appid}>
            <ButtonItem layout="below" onClick={() => openGame(g)} description={describeGame(g)}>
              {g.backup_count > 0 ? "💾 " : ""}
              {g.name}
              {g.kind === "nonsteam" ? " (não-Steam)" : ""}
            </ButtonItem>
          </PanelSectionRow>
        ))}
        {!showAll && !filter.trim() && filtered.length > PAGE_SIZE && (
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => setShowAll(true)}>
              Mostrar todos ({filtered.length})
            </ButtonItem>
          </PanelSectionRow>
        )}
        {data && data.games.length === 0 && !error && (
          <PanelSectionRow>
            <Field description="Nenhum jogo instalado encontrado." />
          </PanelSectionRow>
        )}
      </PanelSection>

      {upd?.error && upd.error.includes("token") && (
        <PanelSection title="Token do GitHub">
          <PanelSectionRow>
            <TextField
              label={upd.has_token ? "Novo token (já existe um salvo)" : "Token (somente leitura)"}
              bIsPassword
              value={token}
              onChange={(e) => setToken(e.target.value)}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={!token.trim() || checking} onClick={() => void saveToken()}>
              Salvar token e verificar
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}

      {data && data.orphans.length > 0 && (
        <PanelSection title="Backups de jogos não instalados">
          <PanelSectionRow>
            <Field description="Instale o jogo, abra-o uma vez e ele aparece na lista acima com a opção Restaurar." />
          </PanelSectionRow>
          {data.orphans.map((o) => (
            <PanelSectionRow key={o.appid}>
              <Field
                label={o.name}
                description={`${o.backup_count} backup(s) · último: ${fmtDate(o.last_backup)}`}
              />
            </PanelSectionRow>
          ))}
        </PanelSection>
      )}
    </>
  );
};

const UPDATE_CHECK_EVERY_MS = 6 * 60 * 60 * 1000;

const notifyIfUpdate = async () => {
  try {
    const r = await updateStatusCall(false);
    if (r.available && !r.notified) {
      toaster.toast({
        title: "SaveBackupSD: atualização disponível",
        body: `v${r.current} → v${r.latest}. Baixe o zip em Releases no GitHub.`,
        duration: 10000,
      });
      await ackUpdateCall(r.latest);
    }
  } catch {
    // sem internet / backend indisponível: tenta de novo no próximo ciclo
  }
};

export default definePlugin(() => {
  const firstCheck = window.setTimeout(() => void notifyIfUpdate(), 20000);
  const periodic = window.setInterval(() => void notifyIfUpdate(), UPDATE_CHECK_EVERY_MS);

  // Escuta fora do painel para avisar mesmo com o menu rápido fechado.
  const autoListener = addEventListener<[appid: number, name: string, ok: boolean, message: string]>(
    "auto_backup",
    (_appid, name, ok, message) =>
      toaster.toast({
        title: ok ? `Backup automático: ${name}` : `Falha no backup: ${name}`,
        body: message,
      })
  );

  return {
    name: "SaveBackupSD",
    titleView: <div className={staticClasses.Title}>Save Backup SD</div>,
    content: <Content />,
    icon: <FaSdCard />,
    onDismount() {
      window.clearTimeout(firstCheck);
      window.clearInterval(periodic);
      removeEventListener("auto_backup", autoListener);
    },
  };
});
