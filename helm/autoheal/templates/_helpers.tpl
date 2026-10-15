{{/* Resource name for a component: <release>-<component> */}}
{{- define "autoheal.name" -}}
{{- printf "%s-%s" .root.Release.Name .component | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "autoheal.selectorLabels" -}}
app.kubernetes.io/name: autoheal
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "autoheal.labels" -}}
{{ include "autoheal.selectorLabels" . }}
app.kubernetes.io/part-of: autoheal
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .root.Chart.Name .root.Chart.Version }}
{{- end -}}

{{- define "autoheal.image" -}}
{{- $img := printf "%s%s:%s" .root.Values.image.repositoryPrefix .component .root.Values.image.tag -}}
{{- if .root.Values.image.registry -}}{{ printf "%s/%s" .root.Values.image.registry $img }}{{- else -}}{{ $img }}{{- end -}}
{{- end -}}

{{- define "autoheal.secretName" -}}{{ .Release.Name }}-secrets{{- end -}}

{{/* Restricted Pod Security Standard compliant contexts. */}}
{{- define "autoheal.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "autoheal.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: [ALL]
{{- end -}}

{{/* Kubernetes-native self-healing: the three probes. */}}
{{- define "autoheal.probes" -}}
{{- $p := .Values.probes -}}
startupProbe:
  httpGet: {path: /health/startup, port: http}
  periodSeconds: {{ $p.startup.periodSeconds }}
  failureThreshold: {{ $p.startup.failureThreshold }}
  timeoutSeconds: {{ $p.startup.timeoutSeconds }}
livenessProbe:
  httpGet: {path: /health/live, port: http}
  periodSeconds: {{ $p.liveness.periodSeconds }}
  failureThreshold: {{ $p.liveness.failureThreshold }}
  timeoutSeconds: {{ $p.liveness.timeoutSeconds }}
readinessProbe:
  httpGet: {path: /health/ready, port: http}
  periodSeconds: {{ $p.readiness.periodSeconds }}
  failureThreshold: {{ $p.readiness.failureThreshold }}
  timeoutSeconds: {{ $p.readiness.timeoutSeconds }}
{{- end -}}

{{/* Env shared by all Python services. */}}
{{- define "autoheal.commonEnv" -}}
- name: POD_NAME
  valueFrom: {fieldRef: {fieldPath: metadata.name}}
- name: POD_NAMESPACE
  valueFrom: {fieldRef: {fieldPath: metadata.namespace}}
- name: CHAOS_TOKEN
  valueFrom: {secretKeyRef: {name: {{ include "autoheal.secretName" . }}, key: chaos-token}}
- name: REDIS_PASSWORD
  valueFrom: {secretKeyRef: {name: {{ include "autoheal.secretName" . }}, key: redis-password}}
- name: REDIS_URL
  value: "redis://:$(REDIS_PASSWORD)@{{ .Release.Name }}-redis:6379/0"
{{- end -}}

{{/* Spread replicas across nodes so a node failure can't take out a whole tier. */}}
{{- define "autoheal.spread" -}}
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: ScheduleAnyway
    labelSelector:
      matchLabels:
        {{- include "autoheal.selectorLabels" . | nindent 8 }}
{{- end -}}

{{/* NetworkPolicy peers. */}}
{{- define "autoheal.peer" -}}
- podSelector:
    matchLabels:
      app.kubernetes.io/instance: {{ .root.Release.Name }}
      app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "autoheal.monitoringPeer" -}}
- namespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: {{ .Values.networkPolicy.monitoringNamespace }}
{{- end -}}

{{/* Pod template annotations: roll pods when config changes. */}}
{{- define "autoheal.podAnnotations" -}}
checksum/config: {{ include (print .Template.BasePath "/configmap.yaml") . | sha256sum }}
{{- end -}}
