import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { useState } from "react";
import { api } from "./lib/api";
import { useAsync } from "./lib/hooks";
import { Button, cx } from "./components/ui";

import { AnswerBank } from "./pages/AnswerBank";
import { Facts } from "./pages/Facts";
import { Outbound } from "./pages/Outbound";
import { Sessions } from "./pages/Sessions";
import { Preferences } from "./pages/Preferences";
import { Analytics } from "./pages/Analytics";
import { Applications } from "./pages/Applications";
import { Campaigns } from "./pages/Campaigns";
import { Jobs } from "./pages/Jobs";
import { ProfilePage } from "./pages/Profile";
import { Queue } from "./pages/Queue";
import { SettingsPage } from "./pages/Settings";
import { Templates } from "./pages/Templates";

const NAV = [
  { to: "/queue", label: "Queue" },
  { to: "/jobs", label: "Jobs" },
  { to: "/applications", label: "Applications" },
  { to: "/analytics", label: "Analytics" },
  { to: "/campaigns", label: "Campaigns" },
  { to: "/facts", label: "Facts" },
  { to: "/answers", label: "Answer bank" },
  { to: "/preferences", label: "Preferences" },
  { to: "/templates", label: "Templates" },
  { to: "/profile", label: "Profile" },
  { to: "/outbound", label: "Outbound" },
  { to: "/sessions", label: "Sessions" },
  { to: "/settings", label: "Settings" },
];

/** The emergency brake, in the header of every page.
 *
 *  Creating data/STOP is what the apply guardrails read on every submit
 *  decision, so this button stops applications immediately — including one
 *  already mid-form. It is deliberately the loudest control in the app. */
function StopControl() {
  const { data, reload } = useAsync(() => api.controlState(), []);
  const [busy, setBusy] = useState(false);

  const stopped = data?.stopped ?? false;

  const toggle = async () => {
    if (!stopped) {
      const confirmed = window.confirm(
        "Stop all applications now?\n\n" +
          "This creates data/STOP, which every guardrail check reads. Nothing " +
          "will be submitted until you resume.",
      );
      if (!confirmed) return;
    }
    setBusy(true);
    try {
      if (stopped) await api.resumeEverything();
      else await api.stopEverything("stopped from the dashboard");
      reload();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex items-center gap-3">
      <span
        className={cx(
          "inline-flex items-center gap-1.5 text-xs",
          stopped ? "text-bad" : "text-good",
        )}
      >
        <span
          className={cx(
            "inline-block h-2 w-2 rounded-full",
            stopped ? "bg-bad" : "bg-good",
          )}
        />
        {stopped ? "STOPPED" : "Running"}
      </span>
      <Button
        variant={stopped ? "primary" : "danger"}
        onClick={toggle}
        disabled={busy}
        className="px-4 py-2 text-sm tracking-wide uppercase"
      >
        {stopped ? "Resume" : "Stop everything"}
      </Button>
    </div>
  );
}

function LiveSubmitIndicator() {
  const { data } = useAsync(() => api.getSettings(), []);
  if (!data) return null;

  return data.allow_live_submit ? (
    <span className="rounded border border-warn/40 bg-warn/10 px-2 py-1 text-xs text-warn">
      LIVE SUBMIT ON
    </span>
  ) : (
    <span
      className="rounded border border-ink-700 bg-ink-850 px-2 py-1 text-xs text-ink-400"
      title="ALLOW_LIVE_SUBMIT is false. Nothing can be submitted. Set it in .env to enable."
    >
      Dry run only
    </span>
  );
}

export default function App() {
  return (
    <div className="flex min-h-full flex-col">
      <header className="sticky top-0 z-10 flex items-center gap-4 border-b border-ink-800 bg-ink-900 px-4 py-2.5">
        <span className="text-sm font-bold tracking-tight text-ink-100">JobSeekr</span>
        <LiveSubmitIndicator />
        <div className="ml-auto">
          <StopControl />
        </div>
      </header>

      <div className="flex flex-1">
        <nav className="w-44 shrink-0 border-r border-ink-800 bg-ink-900 p-2">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                cx(
                  "block rounded px-3 py-1.5 text-sm transition-colors",
                  isActive
                    ? "bg-accent/15 font-medium text-accent"
                    : "text-ink-300 hover:bg-ink-800 hover:text-ink-100",
                )
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>

        <main className="min-w-0 flex-1 p-4">
          <Routes>
            <Route path="/" element={<Navigate to="/queue" replace />} />
            <Route path="/queue" element={<Queue />} />
            <Route path="/jobs" element={<Jobs />} />
            <Route path="/applications" element={<Applications />} />
            <Route path="/analytics" element={<Analytics />} />
            <Route path="/campaigns" element={<Campaigns />} />
            <Route path="/facts" element={<Facts />} />
            <Route path="/answers" element={<AnswerBank />} />
            <Route path="/preferences" element={<Preferences />} />
            <Route path="/templates" element={<Templates />} />
            <Route path="/profile" element={<ProfilePage />} />
            <Route path="/outbound" element={<Outbound />} />
            <Route path="/sessions" element={<Sessions />} />
            <Route path="/settings" element={<SettingsPage />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
