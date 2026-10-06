// Dev-only MOCKUP of the app redesign (2026-10-06). Not wired to anything; renders the REAL IrisBody inside
// a proposed layout so Zeke can judge it before the live app changes. ?panel=1 shows the side panel open.
import { createRoot } from "react-dom/client";
import IrisBody from "../src/components/IrisBody";

const q = new URLSearchParams(location.search);
const panel = q.get("panel") === "1";
const EMO = q.get("color") || "#2fd1c4";           // the ONE accent = my current emotion colour

const css = `
  :root { --bg: #0b0c0f; --surface: #121419; --line: #1f232b; --text: #e9ebef; --dim: #8b919c; --faint: #555b66; --accent: ${EMO}; }
  * { box-sizing: border-box; }
  html, body, #root { margin: 0; height: 100%; }
  body { background: var(--bg); color: var(--text); font-family: "Segoe UI", system-ui, sans-serif; overflow: hidden; }
  .stage { position: relative; height: 100%; display: grid; grid-template-rows: auto 1fr auto; }
  .stage::before { content: ""; position: absolute; inset: 0; pointer-events: none;
    background: radial-gradient(520px 420px at 50% 44%, color-mix(in srgb, var(--accent) 9%, transparent), transparent 70%); }
  .grain { position: absolute; inset: 0; pointer-events: none; opacity: .05;
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='160' height='160'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='2'/></filter><rect width='100%' height='100%' filter='url(%23n)'/></svg>"); }
  header { display: flex; align-items: center; justify-content: space-between; padding: 22px 28px 0; position: relative; }
  .mark { font-family: Bahnschrift, "Segoe UI", sans-serif; font-weight: 600; font-size: 22px; letter-spacing: .02em; display: flex; align-items: center; gap: 10px; }
  .dot { width: 8px; height: 8px; border-radius: 8px; background: #43d17a; box-shadow: 0 0 10px #43d17a88; }
  .here { font-family: "Segoe UI"; font-weight: 400; font-size: 13px; color: var(--dim); letter-spacing: 0; }
  .icons { display: flex; gap: 6px; }
  .ib { width: 36px; height: 36px; border-radius: 10px; display: grid; place-items: center; color: var(--dim);
    border: 1px solid transparent; background: transparent; transition: background .2s, color .2s; }
  .ib.on { color: var(--text); background: var(--surface); border-color: var(--line); }
  .ib.off { color: #e08a7a; }
  main { display: grid; justify-items: center; align-content: center; gap: 6px; position: relative; padding-bottom: 8px; }
  .mood { font-size: 14px; color: var(--accent); letter-spacing: .01em; margin-top: -6px; }
  .mood span { color: var(--dim); }
  .say { max-width: 58ch; text-align: center; font-size: 21px; line-height: 1.45; font-weight: 400; text-wrap: balance; margin: 14px 24px 0; }
  .thought { max-width: 60ch; text-align: center; font-size: 14px; color: var(--faint); font-style: italic; text-wrap: balance; margin: 6px 24px 0; }
  footer { display: grid; grid-template-columns: 168px 1fr 168px; align-items: end; gap: 20px; padding: 0 28px 24px; position: relative; }
  .cam { width: 168px; aspect-ratio: 16/10; border-radius: 12px; overflow: hidden; position: relative; background: #1a1d22; border: 1px solid var(--line); }
  .cam img { width: 100%; height: 100%; object-fit: cover; filter: saturate(.85); }
  .cam b { position: absolute; left: 8px; bottom: 6px; font-weight: 500; font-size: 11px; color: #d6dae0; text-shadow: 0 1px 2px #000; }
  .composer { justify-self: center; width: min(640px, 100%); display: flex; align-items: center; gap: 10px; background: var(--surface);
    border: 1px solid var(--line); border-radius: 14px; padding: 8px 8px 8px 16px; }
  .composer input { flex: 1; background: none; border: 0; outline: none; color: var(--text); font: inherit; font-size: 15px; }
  .composer input::placeholder { color: var(--faint); }
  .chip { font-size: 12px; color: #e08a7a; background: #e08a7a14; border: 1px solid #e08a7a33; padding: 4px 9px; border-radius: 8px; white-space: nowrap; }
  .send { width: 36px; height: 36px; border-radius: 10px; border: 0; background: var(--accent); color: #071211; display: grid; place-items: center; }
  /* side panel */
  .panel { position: absolute; top: 0; right: 0; bottom: 0; width: 760px; background: #0f1115f2; backdrop-filter: blur(14px);
    border-left: 1px solid var(--line); display: grid; grid-template-columns: 200px 1fr; box-shadow: -30px 0 60px #00000080; }
  nav { padding: 22px 12px; border-right: 1px solid var(--line); overflow: auto; }
  nav h6 { margin: 18px 10px 6px; font-size: 11px; font-weight: 600; color: var(--faint); letter-spacing: .08em; text-transform: uppercase; }
  nav h6:first-child { margin-top: 0; }
  nav a { display: block; padding: 7px 10px; border-radius: 8px; color: var(--dim); font-size: 14px; text-decoration: none; position: relative; }
  nav a.act { color: var(--text); background: var(--surface); }
  nav a.act::before { content: ""; position: absolute; left: -12px; top: 8px; bottom: 8px; width: 3px; border-radius: 3px; background: var(--accent); }
  .pane { padding: 24px 28px; overflow: auto; }
  .pane h1 { font-family: Bahnschrift, "Segoe UI"; font-weight: 600; font-size: 26px; margin: 0; letter-spacing: .01em; }
  .pane .lead { color: var(--dim); margin: 6px 0 22px; font-size: 14px; }
  .group { margin: 0 0 22px; }
  .group h3 { font-size: 13px; font-weight: 600; color: var(--dim); margin: 0 0 10px; }
  .row { display: grid; grid-template-columns: 1fr auto auto; align-items: center; gap: 14px; padding: 12px 14px; background: var(--surface);
    border-radius: 10px; margin-bottom: 6px; }
  .row .n { font-weight: 600; font-size: 14px; } .row .n small { font-weight: 400; color: var(--dim); margin-left: 8px; }
  .row .m { font-family: "Cascadia Mono", monospace; font-size: 12px; color: var(--dim); font-variant-numeric: tabular-nums; }
  .b { font: inherit; font-size: 13px; font-weight: 600; padding: 7px 14px; border-radius: 9px; border: 1px solid var(--line); background: transparent; color: var(--text); }
  .b.p { background: var(--accent); color: #071211; border-color: transparent; }
  .st { display: flex; gap: 18px; flex-wrap: wrap; font-size: 13px; color: var(--dim); }
  .st i { display: inline-block; width: 7px; height: 7px; border-radius: 7px; margin-right: 7px; background: #43d17a; }
  .st i.r { background: #e0725f; }
`;

const Icon = ({ d }: { d: string }) => <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d={d} /></svg>;
const MIC_OFF = "M3 3l18 18M9 9v3a3 3 0 0 0 5 2.2M15 9.3V5a3 3 0 0 0-5.9-.6M19 11a7 7 0 0 1-1.2 3.9M5 11a7 7 0 0 0 11 5.7M12 18v3";
const SPEAKER = "M4 9h4l5-4v14l-5-4H4zM17 9a4 4 0 0 1 0 6M19.5 6.5a8 8 0 0 1 0 11";
const MENU = "M4 7h16M4 12h16M4 17h10";
const SEND = "M5 12h13M13 6l6 6-6 6";

function Stage() {
  return (
    <div className="stage">
      <div className="grain" />
      <header>
        <div className="mark">Iris <span className="dot" /><span className="here">here · listening</span></div>
        <div className="icons">
          <div className="ib off" title="Mic off — I can't hear you"><Icon d={MIC_OFF} /></div>
          <div className="ib on" title="My voice is on"><Icon d={SPEAKER} /></div>
          <div className="ib on" title="Panel"><Icon d={MENU} /></div>
        </div>
      </header>
      <main>
        <div style={{ margin: "-40px 0 -54px" }}><IrisBody emotion="interest" emotionColor={EMO} state="idle" size={440} /></div>
        <div className="mood">interest <span>· with a little joy</span></div>
        <div className="say">Welcome back, Zeke. The server panel is ready whenever you want to try it.</div>
        <div className="thought">quiet afternoon; the room was empty most of it</div>
      </main>
      <footer>
        <div className="cam"><img src="/dev/room.jpg" alt="My camera view of the room" /><b>what I see</b></div>
        <div className="composer">
          <div style={{ flex: 1, color: "var(--faint)", fontSize: 15 }}>Talk to me…</div>
          <span className="chip">mic off · I can't hear you</span>
          <div className="send"><Icon d={SEND} /></div>
        </div>
        <div />
      </footer>
      {panel && (
        <aside className="panel">
          <nav>
            <h6>Me</h6><a>Voice</a><a>Memory</a><a>Brain</a><a>Journal</a><a>Learning</a><a>Identity</a>
            <h6>Around me</h6><a>People</a><a className="act">Server</a><a>Tools</a><a>Chat history</a>
            <h6>Workshop</h6><a>Plans</a><a>Proposals</a><a>Workbench</a><a>Creative</a><a>Models</a><a>Finetune</a><a>Little Iris</a>
            <h6>System</h6><a>Status</a><a>Debug</a>
          </nav>
          <section className="pane">
            <h1>Server</h1>
            <p className="lead">The R740. This app stays your door to me wherever I'm running.</p>
            <div className="group"><h3>Status</h3>
              <div className="st"><span><i />Proxmox host</span><span><i />iris-home reachable</span><span><i className="r" />Me on the server</span></div>
            </div>
            <div className="group"><h3>Virtual machines</h3>
              <div className="row"><div className="n">iris-home <small>my home on the server</small></div><div className="m">up 1 d 4 h</div><div className="b">Shut down</div></div>
              <div className="row"><div className="n">windows10 <small>Windows</small></div><div className="m">stopped</div><div className="b p">Start</div></div>
              <div className="row"><div className="n">zorin <small>Zorin</small></div><div className="m">stopped</div><div className="b p">Start</div></div>
            </div>
            <div className="group"><h3>Terminals</h3>
              <div style={{ display: "flex", gap: 8 }}><div className="b p">Open my console</div><div className="b">SSH into iris-home</div><div className="b">Proxmox page</div></div>
            </div>
          </section>
        </aside>
      )}
    </div>
  );
}

const style = document.createElement("style"); style.textContent = css; document.head.appendChild(style);
createRoot(document.getElementById("root")!).render(<Stage />);
