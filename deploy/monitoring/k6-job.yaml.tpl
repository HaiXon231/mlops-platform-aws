apiVersion: batch/v1
kind: Job
metadata:
  generateName: m33-k6-
  namespace: mlops
  labels:
    app: m33-k6
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 1200
  ttlSecondsAfterFinished: 3600
  template:
    metadata:
      labels:
        app: m33-k6
    spec:
      restartPolicy: Never
      nodeSelector:
        eks.amazonaws.com/nodegroup: system
      containers:
        - name: k6
          image: grafana/k6:1.8.2
          command: [k6, run, /scripts/k6.js]
          env:
            - name: LOAD_VUS
              value: "${LOAD_VUS}"
            - name: LOAD_RATE
              value: "${LOAD_RATE}"
            - name: LOAD_DURATION
              value: "${LOAD_DURATION}"
            - name: LOAD_BASE_URL
              value: "${LOAD_BASE_URL}"
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
              ephemeral-storage: 64Mi
            limits:
              cpu: 500m
              memory: 512Mi
              ephemeral-storage: 256Mi
          volumeMounts:
            - name: script
              mountPath: /scripts
              readOnly: true
      volumes:
        - name: script
          configMap:
            name: m33-k6-script
