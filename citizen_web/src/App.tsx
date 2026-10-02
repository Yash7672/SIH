import { lazy, Suspense } from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import Layout from "./components/Layout";
import { PageSkeleton } from "./components/ui/Feedback";
import { PageBackground } from "./components/ui/PageBackground";

// Route-level code splitting keeps the initial bundle small for citizens on
// slow mobile connections: a first-time visitor downloads the shell plus the
// login screen only, then each page arrives as it is needed.
const Login = lazy(() => import("./pages/Login"));
const Register = lazy(() => import("./pages/Register"));
const Dashboard = lazy(() => import("./pages/Dashboard"));
const NewComplaint = lazy(() => import("./pages/NewComplaint"));
const MyComplaints = lazy(() => import("./pages/MyComplaints"));
const ComplaintDetail = lazy(() => import("./pages/ComplaintDetail"));

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
      <PageBackground>
        <Suspense fallback={<PageSkeleton />}>
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
        </Suspense>
      </PageBackground>
    </BrowserRouter>
  );
}
