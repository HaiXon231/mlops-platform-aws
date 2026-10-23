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
            - name: K6_VUS
              value: "${K6_VUS}"
            - name: K6_RATE
              value: "${K6_RATE}"
            - name: K6_DURATION
              value: "${K6_DURATION}"
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
