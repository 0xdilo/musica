const select = (id) => document.getElementById(id);
let token = sessionStorage.getItem("musica-capture") || "";
let connection = "";
let messageTimer;
const labels = {
  queued: "In coda",
  matching: "Cerco il brano",
  downloading: "Download",
  importing: "Importazione",
  ready: "In libreria",
  failed: "Da riprovare",
};

function notice(message, error = false) {
  clearTimeout(messageTimer);
  select("notice").textContent = message;
  select("notice").classList.toggle("error", error);
  if (!error)
    messageTimer = setTimeout(() => {
      select("notice").textContent = "";
    }, 6000);
}
function disconnect() {
  token = "";
  connection = "";
  sessionStorage.removeItem("musica-capture");
  select("connected").hidden = true;
  select("login-panel").hidden = false;
  select("tracks").replaceChildren();
}
async function request(path, body) {
  let response;
  try {
    response = await fetch(path, {
      method: body ? "POST" : "GET",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
  } catch {
    throw new Error("Connessione non riuscita. Controlla Tailscale e riprova.");
  }
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401) disconnect();
    throw new Error(data.error || "Richiesta non riuscita. Riprova.");
  }
  return data;
}
function renderTrack(track) {
  const row = document.createElement("li");
  row.className = `track ${track.status === "failed" ? "failed" : ""}`;
  const info = document.createElement("div");
  info.className = "track-info";
  const title = document.createElement("h3");
  title.textContent = track.title;
  const artist = document.createElement("p");
  artist.textContent = track.artist;
  info.append(title, artist);
  if (track.status === "failed") {
    const error = document.createElement("p");
    error.textContent = track.error || "Il brano non è stato importato.";
    const retry = document.createElement("button");
    retry.className = "retry secondary";
    retry.textContent = "Riprova";
    retry.addEventListener("click", async () => {
      retry.disabled = true;
      try {
        const data = await request("/api/capture", {
          artist: track.artist,
          title: track.title,
        });
        notice(data.message);
        await refresh();
      } catch (error) {
        notice(error.message, true);
        retry.disabled = false;
      }
    });
    info.append(error, retry);
  }
  const state = document.createElement("span");
  state.className = "track-state";
  state.textContent = labels[track.status] || "In elaborazione";
  row.append(info, state);
  return row;
}
async function refresh() {
  const data = await request("/api/capture/jobs");
  select("tracks").replaceChildren(...data.tracks.map(renderTrack));
  select("empty").hidden = data.tracks.length > 0;
}
async function refreshRadios() {
  const data = await request("/api/radios");
  select("radio-section").hidden = !data.enabled;
  select("radio-list").replaceChildren(
    ...data.stations.map((station) => {
      const row = document.createElement("li");
      row.className = "radio-row";
      const title = document.createElement("span");
      title.textContent = station.name;
      const status = document.createElement("span");
      status.textContent =
        station.published && station.buffered
          ? "Pronta in Amperfy"
          : station.status === "failed"
            ? "Riprovo tra poco"
            : "Preparo i brani…";
      if (station.error) status.title = station.error;
      const info = document.createElement("div");
      info.className = "radio-info";
      info.append(title, status);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "quiet remove-radio";
      remove.textContent = "×";
      remove.setAttribute("aria-label", `Rimuovi radio ${station.name}`);
      remove.addEventListener("click", async () => {
        remove.disabled = true;
        try {
          await request("/api/radios", { remove: station.id });
          await refreshRadios();
          notice("Radio rimossa. La tua musica rimane in libreria.");
        } catch (error) {
          notice(error.message, true);
          remove.disabled = false;
        }
      });
      row.append(info, remove);
      return row;
    }),
  );
  const current = select("radio-playlist").value;
  select("radio-playlist").replaceChildren(
    ...data.playlists.map((playlist) => {
      const option = document.createElement("option");
      option.value = playlist.id;
      option.textContent = `${playlist.name} (${playlist.count})`;
      return option;
    }),
  );
  if (current) select("radio-playlist").value = current;
}
select("radio-source").addEventListener("change", () => {
  const source = select("radio-source").value;
  select("radio-genre-field").hidden = source !== "genre";
  select("radio-playlist-field").hidden = source !== "playlist";
});
select("radio-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  select("create-radio").disabled = true;
  try {
    const mode = select("radio-source").value;
    const value =
      mode === "genre"
        ? select("radio-genre").value.trim()
        : mode === "playlist"
          ? select("radio-playlist").value
          : "";
    await request("/api/radios", { context: { mode, value } });
    await refreshRadios();
    notice(
      "Radio configurata. Comparirà in Amperfy appena i primi brani sono pronti.",
    );
  } catch (error) {
    notice(error.message, true);
  } finally {
    select("create-radio").disabled = false;
  }
});
async function connect() {
  const config = await request("/api/capture/config");
  connection = config.connection;
  select("library-url").value = config.library_url;
  select("login-panel").hidden = true;
  select("connected").hidden = false;
  await Promise.all([refresh(), refreshRadios()]);
}
select("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  select("login").disabled = true;
  try {
    const data = await request("/api/capture/pair", {
      username: select("username").value,
      password: select("password").value,
    });
    select("password").value = "";
    token = data.token;
    sessionStorage.setItem("musica-capture", token);
    await connect();
    notice("Dispositivo collegato. Ora aggiungi il comando.");
  } catch (error) {
    notice(error.message, true);
  } finally {
    select("login").disabled = false;
  }
});
async function copy(value, message) {
  try {
    await navigator.clipboard.writeText(value);
    notice(message);
  } catch {
    notice(
      "Safari non ha consentito la copia. Apri la pagina con HTTPS e riprova.",
      true,
    );
  }
}
select("copy").addEventListener("click", () =>
  copy(
    connection,
    "Configurazione copiata. Incollala quando aggiungi il comando.",
  ),
);
select("copy-server").addEventListener("click", () =>
  copy(select("library-url").value, "Indirizzo copiato."),
);
select("logout").addEventListener("click", disconnect);
select("refresh").addEventListener("click", () =>
  refresh().catch((error) => notice(error.message, true)),
);
setInterval(() => {
  if (token && !document.hidden)
    refresh().catch((error) => notice(error.message, true));
}, 15000);
if (token) connect().catch((error) => notice(error.message, true));

setInterval(() => {
  if (token && !document.hidden)
    refreshRadios().catch((error) => notice(error.message, true));
}, 30000);
