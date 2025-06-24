# 🚀 LiveKit Service 🚀

Welcome to the LiveKit Service! This is your gateway to real-time video and audio magic. Follow these steps to get started on your adventure.

## 🛠️ Getting Started

Ready to bring your application to life? Let's get this party started!

1.  **Install Dependencies**
    
    First things first, let's get all the necessary packages installed. Open your terminal and run:
    
    ```bash
    pip install -r requirements.txt
    ```

2.  **Download Necessary Files**
    
    Next, let's grab some essential files for the service to run properly.
    
    ```bash
    python main.py download-files
    ```

3.  **Generate gRPC Code**
    
    Now, we need to generate some code from our protobuf definitions. This allows our services to communicate.
    
    ```bash
    python -m grpc_tools.protoc -I./pronity-frontend/protos --python_out=./livekit-service/rox/generated/protos --pyi_out=./livekit-service/rox/generated/protos --grpc_python_out=./livekit-service/rox/generated/protos ./pronity-frontend/protos/interaction.proto
    ```

4.  **Run the Service**
    
    You're all set! Start the service with:
    
    ```bash
    python main.py
    ```

And that's it! You're ready to go. Happy coding! 🎉