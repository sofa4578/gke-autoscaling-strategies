# GKE Autoscaling Strategies

Comparative implementation and load-test evaluation of three autoscaling strategies for a
containerised HTTP service on Google Kubernetes Engine.

Built as a bachelor's thesis project (Computer Science, Lviv Polytechnic National University).
The infrastructure is provisioned from scratch with Terraform and deployed through GitHub Actions.

---

## Problem

Kubernetes' default `HorizontalPodAutoscaler` scales on CPU utilisation. For many real workloads
CPU is a lagging and indirect signal: a service can be saturated on I/O, or holding a deep task
queue, while CPU stays low. The scaling decision then arrives after users have already seen
latency.

This project implements three different scaling triggers against the same application and the
same load profile, and measures how each one behaves.

| # | Strategy | Trigger | Scales on | Manifest |
|---|----------|---------|-----------|----------|
| 1 | CPU-based HPA | CPU utilisation > 50% | Resource consumption | `k8s/hpa-cpu.yaml` |
| 2 | KEDA + Prometheus | HTTP requests/sec > 50 | Incoming demand | `k8s/keda-rps.yaml` |
| 3 | KEDA + Redis | Queue length > 5 tasks | Backlog of work | `k8s/keda-queue.yaml` |

Strategy 2 reacts to load before it becomes CPU pressure. Strategy 3 decouples scaling from the
HTTP path entirely, which is the right model for asynchronous worker pools.

---

## Architecture

```text
Developer
   │  git push
   ▼
GitHub Actions  ──────────────────────────────────┐
   │                                              │
   ├─ 1. Terraform  → GCP (VPC, GKE, Memorystore, Artifact Registry)
   ├─ 2. Docker build → Artifact Registry
   └─ 3. kubectl apply → GKE
                                                  │
GKE cluster (namespace: prod-apps)                │
   ├── Deployment: fastapi-app (2 replicas)  ◀────┘
   ├── Service (LoadBalancer)
   ├── PodDisruptionBudget
   ├── HPA / KEDA ScaledObject   ← one of three strategies
   ├── KEDA operator             (namespace: keda)
   └── Prometheus                (namespace: monitoring)
            ▲
            │ scrapes /metrics
            │
   Memorystore Redis (private VPC peering) ← task queue for strategy 3

Locust (local) ──HTTP──▶ Service external IP
```

**Flow.** Terraform creates the VPC, a zonal GKE cluster, a Memorystore Redis instance reachable
only over private VPC peering, and an Artifact Registry repository. The application image is built
and tagged with the commit SHA, then pushed. Manifests are applied to the `prod-apps` namespace.
Prometheus scrapes the application's `/metrics` endpoint and serves as the external metrics source
for KEDA strategy 2. Redis serves as both the task backend and the scaling signal for strategy 3.
Load is generated from Locust against the Service's external address.

---

## Technology stack

| Technology | Purpose | Why |
|---|---|---|
| GKE | Managed Kubernetes | Removes control-plane operation from scope so the study can focus on scaling behaviour |
| Terraform | Infrastructure as code | Reproducible teardown and rebuild between test runs; state in GCS |
| Memorystore (Redis) | Task queue and scaling signal | Managed, private-IP only, no queue infrastructure to operate |
| KEDA | Event-driven autoscaling | Native HPA cannot scale on Prometheus queries or queue depth without an adapter |
| Prometheus | Metrics and external metrics API | Provides the RPS signal KEDA consumes for strategy 2 |
| FastAPI | Test application | Async ASGI, trivially instrumented, low baseline overhead so the load signal is not masked |
| Locust | Load generation | Scriptable in Python, produces per-request latency distributions |
| GitHub Actions | CI/CD | Provisions infrastructure, builds images, deploys, and can switch strategies on demand |

---

## Deployment design

The Deployment is configured for zero-downtime rollouts. Three settings work together:

- `maxUnavailable: 0` with `maxSurge: 1` — a new pod must be Ready before an old one is removed,
  so capacity never drops below the declared replica count during a rollout.
- `preStop` hook sleeping 15s — when a pod is marked for deletion, Kubernetes removes it from
  Service endpoints and sends `SIGTERM` at the same time. The sleep holds the container open long
  enough for the endpoint removal to propagate to every kube-proxy, so in-flight requests are not
  dropped.
- `terminationGracePeriodSeconds: 60` — must exceed the preStop sleep plus the application's own
  drain time, otherwise the kubelet issues `SIGKILL` mid-request.

A `PodDisruptionBudget` protects availability during voluntary disruptions such as node upgrades.

Liveness and readiness probes use deliberately different timings: readiness is fast and
aggressive (5s period) so traffic is withheld quickly, while liveness is slower (10s period,
20s initial delay) so a briefly busy pod is not restarted unnecessarily.

Resource requests (250m CPU, 128Mi) are set below limits (1000m CPU, 512Mi). The request value is
what the CPU-based HPA computes utilisation against, so it directly determines strategy 1's
behaviour.

---

## Results

[Replace this section with your actual numbers. The Locust HTML reports are in `scripts/`.
For each strategy, state: time from load start to first scale-up, peak replica count,
p95 latency during the scaling window, and whether any requests failed. A table plus two or
three screenshots of the Locust charts is enough. This is the most valuable section in the
document — a reviewer will read it before anything else.]

| Strategy | Time to first scale-up | Peak replicas | p95 latency under load | Failed requests |
|---|---|---|---|---|
| CPU HPA | | | | |
| KEDA RPS | | | | |
| KEDA queue | | | | |

**Conclusion.** [One paragraph: which strategy responded fastest, which was most stable, and
which you would choose for which workload shape.]

---

## Repository layout

```text
.
├── app/                     FastAPI application, multi-stage Dockerfile
├── k8s/                     Namespace, Deployment, Service, ConfigMap, Secret, PDB,
│                            and the three scaling manifests
├── terraform/               Root module + modules/{vpc,gke,redis}, GCS remote state
├── monitoring/              Prometheus and Grafana Helm values
├── scripts/                 Locust load profiles, Redis queue producer, test reports
└── .github/workflows/       deploy.yml, destroy.yml, switch-approach.yml
```

---

## Running it

Prerequisites: a GCP project with billing enabled, a GCS bucket for Terraform state, and
`gcloud`, `terraform`, `kubectl` and `helm` installed locally.

1. Create the state bucket and set its name in `terraform/main.tf`.
2. Add a `GCP_CREDENTIALS` secret in the repository settings.
3. Push to `main`, or trigger **Build & Deploy** manually from the Actions tab.
4. Switch strategies with the **switch-approach** workflow.
5. Tear everything down with the **destroy** workflow. Memorystore and GKE both bill hourly.

Load testing:

```bash
cd scripts
locust -f locustfile.py --host http://<service-external-ip>
```

---

## Known limitations and production hardening

This is a study environment, deliberately scoped. The following would all be required before
anything like it ran in production, and are listed here because knowing the gap matters more
than pretending it isn't there.

**CI/CD**
- `terraform apply -auto-approve` runs without a `plan` review gate. Production practice is
  `plan` on pull request with the diff posted for review, and `apply` only on merge behind a
  protected GitHub Environment with a required reviewer.
- Authentication uses a long-lived service-account JSON key. Workload Identity Federation
  issues short-lived tokens to Actions instead and removes the static credential entirely.
- No image scanning, no dependency scanning, no Terraform policy checks. Trivy, Checkov and
  `tflint` belong in the pipeline as required checks.

**Secrets**
- The Redis auth string is read from Terraform output and templated into a Secret manifest at
  deploy time. It is masked in logs, but it should not leave GCP at all: the correct shape is
  Secret Manager plus External Secrets Operator, or a Workload-Identity-bound Kubernetes
  service account.
- The deploy job currently continues with Redis AUTH disabled if the auth string comes back
  empty. A deployment step should fail closed, not fall back to a weaker configuration.

**Container and cluster security**
- The container runs as root with a writable root filesystem. It should declare
  `runAsNonRoot`, a non-zero `runAsUser`, `readOnlyRootFilesystem`, and drop all capabilities.
- Base images are tagged, not pinned by digest, so builds are not fully reproducible.
- No RBAC scoping, NetworkPolicies, or Pod Security Standards enforcement on the namespace.

**Networking**
- The application is exposed through a `LoadBalancer` Service with no TLS and no DNS. An
  Ingress with cert-manager would terminate HTTPS and allow host-based routing.

**Observability**
- Prometheus collects metrics but there are no alerting rules, no Alertmanager, and no
  dashboards stored as code. There is no log aggregation.

**Reliability**
- The GKE cluster is zonal, so a zone failure takes the whole service down. A regional cluster
  with node pools across three zones would be the production choice.
- No backup or restore procedure for Redis state.

## License

MIT
