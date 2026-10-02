import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import { AlertEvent, fetchRecentAlerts, mergeAlerts } from "./services/api";
import { PageSkeleton } from "./components/ui/Feedback";

// Route-level code splitting: the map/chart libraries (leaflet, recharts) are
// only downloaded when the officer opens those pages.
const Login = lazy(() => import("./pages/Login"));
const Overview = lazy(() => import("./pages/Overview"));
const Complaints = lazy(() => import("./pages/Complaints"));
const Hotlist = lazy(() => import("./pages/Hotlist"));
const LiveAlerts = lazy(() => import("./pages/LiveAlerts"));
const VehicleDetail = lazy(() => import("./pages/VehicleDetail"));
const VehicleSearch = lazy(() => import("./pages/VehicleSearch"));
const Analytics = lazy(() => import("./pages/Analytics"));
const Admin = lazy(() => import("./pages/Admin"));

/** Hard cap: a busy hour must not grow the DOM without bound. */
const MAX_ALERTS = 100;
/** Alerts arriving in the same tick are flushed together, at most every 250ms. */
const ALERT_FLUSH_MS = 250;

function NotFound() {
  return <Navigate to="/" replace />;
}

function RequireAuth({ children }: { children: JSX.Element }) {
  const token = localStorage.getItem("rakshak_token");
  const user = JSON.parse(localStorage.getItem("rakshak_user") || "null");
  if (!token || !user) return <Navigate to="/login" replace />;
  if (user.role !== "COP" && user.role !== "ADMIN") return <Navigate to="/login" replace />;
  return children;
}

export default function App() {
  const [liveAlerts, setLiveAlerts] = useState<AlertEvent[]>([]);
  const queueRef = useRef<AlertEvent[]>([]);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  /**
   * A busy junction can push a burst of alerts in one tick. Re-rendering the
   * overview, sidebar badge and alert list for each one is wasted work, so
   * messages are queued and flushed on a timer instead of setting state per
   * socket event.
   */
  const pushAlert = useCallback((a: AlertEvent) => {
    queueRef.current.push(a);
    if (timerRef.current) return;
    timerRef.current = setTimeout(() => {
      const batch = queueRef.current.splice(0, queueRef.current.length);
      timerRef.current = null;
      if (batch.length === 0) return;
      setLiveAlerts((prev) => {
        const next = [...batch.reverse(), ...prev];
        // De-duplicate by sighting id in case a reconnect replays messages.
        const seen = new Set<string>();
        return next.filter((item) => {
          const key = item.sighting_id || `${item.plate}-${item.timestamp}`;
          if (seen.has(key)) return false;
          seen.add(key);
          return true;
        }).slice(0, MAX_ALERTS);
      });
    }, ALERT_FLUSH_MS);
  }, []);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  /**
   * Hydrate from GET /alerts on mount.
   *
   * The alert list used to exist only in memory, so a browser refresh wiped it
   * and the operator could not see what had already been detected while they
   * were away. Live events arriving during the fetch are merged by sighting id,
   * so nothing is lost or duplicated at the seam.
   */
  useEffect(() => {
    let cancelled = false;

    fetchRecentAlerts()
      .then((history) => {
        if (cancelled) return;
        setLiveAlerts((prev) => mergeAlerts(prev, history, MAX_ALERTS));
      })
      .catch((error) => {
        // Not fatal: the socket is the primary feed, so the page still goes live.
        // Surfaced in the console because the visible symptom is an empty list.
        if (!cancelled) console.warn("[rakshak] could not load recent alerts:", error);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <Suspense fallback={<PageSkeleton />}>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route
          path="/"
          element={
            <RequireAuth>
              <Layout alerts={liveAlerts} onAlert={pushAlert} />
            </RequireAuth>
          }
        >
          <Route index element={<Overview alerts={liveAlerts} />} />
          <Route path="complaints" element={<Complaints />} />
          <Route path="hotlist" element={<Hotlist />} />
          <Route path="alerts" element={<LiveAlerts alerts={liveAlerts} />} />
          <Route path="search" element={<VehicleSearch />} />
          <Route path="vehicles/:plate" element={<VehicleDetail />} />
          <Route path="analytics" element={<Analytics />} />
          <Route path="admin" element={<Admin />} />
        </Route>
        <Route path="*" element={<NotFound />} />
      </Routes>
    </Suspense>
  );
}
