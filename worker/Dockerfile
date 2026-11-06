# CPU-only multiarch worker. No privileged mode or host Docker socket.
FROM python:3.11-slim-bookworm AS llama-build
ARG LLAMA_CPP_REF=master
RUN apt-get update && apt-get install -y --no-install-recommends \
    git cmake ninja-build g++ libcurl4-openssl-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 https://github.com/ggml-org/llama.cpp.git /src/llama.cpp \
    && cd /src/llama.cpp && git fetch --depth 1 origin "${LLAMA_CPP_REF}" \
    && git checkout FETCH_HEAD \
    && cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
       -DGGML_NATIVE=OFF -DGGML_CUDA=OFF -DLLAMA_CURL=ON \
    && cmake --build build --target llama-server -j 2

FROM python:3.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcurl4 libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=llama-build /src/llama.cpp/build/bin/ /usr/local/bin/
ENV LD_LIBRARY_PATH=/usr/local/bin PYTHONUNBUFFERED=1 HOME=/data HF_HOME=/models/.hf-cache HF_XET_CACHE=/models/.hf-cache/xet
WORKDIR /app
COPY worker/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY compute_catalog.py worker/agent.py /app/
RUN mkdir /data /models && chown -R 10001:10001 /data /models
USER 10001:10001
CMD ["python", "agent.py"]
