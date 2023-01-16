import cv2
import numpy as np
import time

def getVideo():
    cap = cv2.VideoCapture("C:\\Users\\Roboy\\projects\\src\\github.com\\Roboy\\test1.mp4")
    if (cap.isOpened()== False):
        print("Error opening video stream or file")

    while(cap.isOpened()):
        ret, frame = cap.read()
        if ret == True:
            tic = time.perf_counter()
            faces = face_cascade.detectMultiScale(frame[:1536, :1536], 1.3, 5)
            for (x, y, w, h) in faces:
                cv2.rectangle(frame, (x, y), (x+w, y+h), (255, 0, 0), 2)
            toc = time.perf_counter()
            print(toc-tic) # average 0.12 (detection from one camera)
            cv2.imshow('Frame',frame)
            if cv2.waitKey(25) & 0xFF == ord('q'):
                break
        else:
            break

    cap.release()

face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
eye_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_eye.xml')

def faceDetect(frame):
    cv2.fishye.undistortImage(frame.to_ndarray())
    faces = face_cascade.detectMultiScale(frame.to_ndarray(), 1.3, 5)
    return np.array(faces).tolist()


if __name__ == "__main__":
    getVideo()
    print(face_cascade)