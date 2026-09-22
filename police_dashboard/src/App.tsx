import { useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import Login from "./pages/Login";
import Layout from "./components/Layout";
import Overview from "./pages/Overview";
import Complaints from "./pages/Complaints";
import Hotlist from "./pages/Hotlist";
import LiveAlerts from "./pages/LiveAlerts";
import VehicleDetail from "./pages/VehicleDetail";
import VehicleSearch from "./pages/VehicleSearch";
import Analytics from "./pages/Analytics";
import Admin from "./pages/Admin";
import { AlertEvent } from "./services/api";

function RequireAuth({ children }: { children: JSX.Element }) {
  const token = localStorage.getItem("rakshak_token");
  const user = JSON.parse(localStorage.getItem("rakshak_user") || "null");
  if (!token || !user) return <Navigate to="/login" replace />;
  if (user.role !== "COP" && user.role !== "ADMIN") return <Navigate to="/login" replace />;
  return children;
}

export default function App() {
  const [liveAlerts, setLiveAlerts] = useState<AlertEvent[]>([]);

  function pushAlert(a: AlertEvent) {
    setLiveAlerts((prev) => [a, ...prev].slice(0, 50));
  }

  return (
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
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
