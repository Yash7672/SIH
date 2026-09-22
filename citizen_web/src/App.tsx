import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import Login from "./pages/Login";
import Register from "./pages/Register";
import Dashboard from "./pages/Dashboard";
import NewComplaint from "./pages/NewComplaint";
import MyComplaints from "./pages/MyComplaints";
import ComplaintDetail from "./pages/ComplaintDetail";
import Layout from "./components/Layout";

function RequireAuth({ children }: { children: JSX.Element }) {
  const token = localStorage.getItem("rakshak_token");
  const user = JSON.parse(localStorage.getItem("rakshak_user") || "null");
  if (!token || !user) return <Navigate to="/login" replace />;
  if (user.role !== "CITIZEN" && user.role !== "ADMIN")
    return <Navigate to="/login" replace />;
  return children;
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/register" element={<Register />} />
        <Route
          path="/"
          element={
            <RequireAuth>
              <Layout />
            </RequireAuth>
          }
        >
          <Route index element={<Dashboard />} />
          <Route path="complaints/new" element={<NewComplaint />} />
          <Route path="complaints" element={<MyComplaints />} />
          <Route path="complaints/:id" element={<ComplaintDetail />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
