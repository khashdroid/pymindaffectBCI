#!/usr/bin/env python3
#  Copyright (c) 2019 MindAffect B.V. 
#  Author: Jason Farquhar <jason@mindaffect.nl>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
from mindaffectBCI.utopiaclient import UtopiaClient, Subscribe, StimulusEvent, NewTarget, Selection, DataPacket, UtopiaMessage, SignalQuality
from collections import deque
from mindaffectBCI.decoder.utils import RingBuffer, extract_ringbuffer_segment, linear_trend_tracker
from time import sleep
import numpy as np

class UtopiaDataInterface:
    # TODO [X] : infer valid data time-stamps
    # TODO [X] : smooth and de-jitter the data time-stamps
    # TODO [] : expose a (potentially blocking) message generator interface
    # TODO [X] : ring-buffer for the stimulus-state also, so fast random access
    # TODO [X] : rate limit waiting to reduce computational load
    VERBOSITY = 1

    def __init__(self, datawindow_ms=60000, msgwindow_ms=60000,
                 data_preprocessor=None, stimulus_preprocessor=None, send_signalquality=True, 
                 timeout_ms=100, mintime_ms=50, fs=None, U=None):
        # rate control
        self.timeout_ms = timeout_ms
        self.mintime_ms = mintime_ms # minimum time to spend in update => max processing rate
        # amout of data in the ring-buffer
        self.datawindow_ms = datawindow_ms
        self.msgwindow_ms = msgwindow_ms
        # connect to the mindaffectDecoder
        self.host = None
        self.port = -1
        self.U = UtopiaClient() if U is None else U
        self.t0 = self.getTimeStamp()
        # init the buffers

        # Messages
        self.msg_ringbuffer = deque()
        self.msg_timestamp = None # ts of most recent processed message

        # DataPackets
        self.data_ringbuffer = None # init later...
        self.data_timestamp = None # ts of last data packet seen
        self.last_sample_timestamp = None # ts of the last preprocessed packed added to ring buffer
        self.sample2timestamp = None # sample tracker to de-jitter time-stamp information
        self.data_preprocessor = data_preprocessor # function to pre-process the incomming data

        # StimulusEvents
        self.stimulus_ringbuffer = None # init later...
        self.stimulus_timestamp = None # ts of most recent processed data
        self.stimulus_preprocessor = stimulus_preprocessor # function to pre-process the incomming data

        # Info about the data sample rate -- estimated from packet rates..
        self.raw_fs = fs
        self.fs = None
        self.newmsgs = [] # list new unprocssed messages since last update call

        # BODGE: running statistics for sig2noise estimation
        # TODO []: move into it's own Sig2Noise computation class
        self.send_signalquality = send_signalquality
        self.last_sigquality_ts = None
        self.last_log_ts = None
        self.send_sigquality_interval = 1000 # send signal qualities every 1000ms = 1Hz
        self.noise2sig_halflife = (5000,500) # noise2sig estimate halflife
        # TODO [x]: move into a exp-move-ave power est class
        self.raw_power = None
        self.preproc_power = None

    def connect(self, host=None, port=-1, queryifhostnotfound=True):
        '''make a connection to the utopia host'''
        if host:
            self.host = host
        if port > 0:
            self.port = port
        self.U.autoconnect(self.host, self.port, timeout_ms=5000, queryifhostnotfound=queryifhostnotfound)
        if self.U.isConnected:
            # subscribe to messages: data, stim, mode, selection
            self.U.sendMessage(Subscribe(None, "DEMSN"))
        return self.U.isConnected
    
    def isConnected(self):
        return self.U.isConnected if self.U is not None else False

    def getTimeStamp(self):
        '''get the current timeStamp'''
        return self.U.getTimeStamp()

    def sendMessage(self, msg: UtopiaMessage):
        ''' send a UtopiaMessage to the utopia hub'''
        self.U.sendMessage(msg)

    def getNewMessages(self, timeout_ms=0):
        ''' get new messages from the UtopiaHub '''
        return self.U.getNewMessages(timeout_ms)

    def initDataRingBuffer(self):
        '''initialize the data ring buffer, by getting some seed messages and datapackets to get the data sizes etc.'''
        print("geting some initial data to setup the ring buffer")
        # get some initial data to get data shape and sample rate
        databuf = []
        nmsg = 0
        while len(databuf) < 30:
            msgs = self.getNewMessages(500)
            for m in msgs:
                m = self.preprocess_message(m)
                if m.msgID == DataPacket.msgID: # data-packets are special
                    if len(m.samples) > 0:
                        databuf.append(m) # append raw data
                    else:
                        print("Huh? got empty data packet: {}".format(m))
                else:
                    self.msg_ringbuffer.append(m)
                    self.msg_timestamp = m.timestamp
                    nmsg = nmsg+1
        nsamp = sum([len(m.samples) for m in databuf]) - len(databuf[0].samples)
        dur = (databuf[-1].timestamp - databuf[0].timestamp)/1000.0 #[-1, -1]-databuf[0][-1, -1])/1000.0
        if self.raw_fs is None:
            self.raw_fs = nsamp/dur # fs = nSamp/time
        print('Estimated sample rate {} samp in {} s ={}'.format(nsamp,dur,self.raw_fs))

        # init the pre-processor (if one)
        if self.data_preprocessor:
            self.data_preprocessor.fit(np.array(databuf[0].samples), fs=self.raw_fs) # tell it the sample rate

        # apply the data packet pre-processing -- to get the info
        # on the data state after pre-processing
        tmpdatabuf = [self.processDataPacket(m) for m in databuf]
        # strip empty packets
        tmpdatabuf = [ d for d in tmpdatabuf if d.shape[0]>0]
        # estimate the sample rate of the pre-processed data
        pp_nsamp = sum([d.shape[0] for d in tmpdatabuf]) - tmpdatabuf[0].shape[0]
        self.fs = pp_nsamp/dur # fs = nSamp/time
        print('Estimated pre-processed sample rate={}'.format(self.fs))

        # create the ring buffer, big enough to store the pre-processed data
        if self.data_ringbuffer:
            print("Warning: re-init data ring buffer")
        # TODO []: why does the datatype of the ring buffer matter so much? Is it because of uss?
        #  Answer[]: it's the time-stamps, float32 rounds time-stamps to 24bits
        self.data_ringbuffer = RingBuffer(maxsize=self.fs*self.datawindow_ms/1000, shape=tmpdatabuf[0].shape[1:], dtype=np.float32)

        # insert the warmup data into the ring buffer
        self.data_timestamp=None # reset last seen data
        # use linear trend tracker to de-jitter the sample timestamps
        self.sample2timestamp = linear_trend_tracker(halflife=5000)
        for d in databuf:
            # apply the pre-processing again (this time with fs estimated)
            d = self.processDataPacket(d)
            self.data_ringbuffer.extend(d)

        return (nsamp, nmsg)

    def initStimulusRingBuffer(self):
        '''initialize the data ring buffer, by getting some seed messages and datapackets to get the data sizes etc.'''
        # TODO []: more efficient memory use, with different dtype for 'real' data and the time-stamps?
        self.stimulus_ringbuffer = RingBuffer(maxsize=self.fs*self.datawindow_ms/1000, shape=(257,), dtype=np.float32)

    def preprocess_message(self, m:UtopiaMessage):
        ''' apply pre-processing to topia message before any more work '''
        #  WARNING BODGE: fit time-stamp in 24bits for float32 ring buffer
        #  Note: this leads to wrap-arroung in (1<<24)/1000/3600 = 4.6 hours
        #        but that shouldn't matter.....
        m.timestamp = m.timestamp % (1<<24)
        return m
    
    def processDataPacket(self, m: DataPacket):
        '''pre-process a datapacket message ready to be inserted into the ringbuffer'''
        #print("DP: {}".format(m))
        # extract the raw data
        d = np.array(m.samples, dtype=np.float32) # process as singles
        # apply the pre-processor, if one was given

        if self.data_preprocessor:
            d_raw = d.copy()
            # warning-- with agressive downsample this may not produce any data!
            d = self.data_preprocessor.transform(d)

            # BODGE: running estimate of the electrode-quality, ONLY after initialization!
            if self.send_signalquality and self.data_ringbuffer is not None:
                self.update_and_send_ElectrodeQualities(d_raw, d, m.timestamp)

                #if self.VERBOSITY > 0 and self.data_ringbuffer is not None:
                #    self.plot_raw_preproc_data(d_raw,d,m.timestamp)

        if d.size > 0 :
            # If have data to add to the ring-buffer, guarding for time-stamp wrap-around
            # TODO [ ]: de-jitter and better timestamp interpolation
            # guard for wrap-around!
            if self.data_timestamp is not None and m.timestamp < self.data_timestamp:
                print("Warning: Time-stamp wrap-around detected!!")

            d = self.add_sample_timestamps(d,m.timestamp,self.fs)

        # update the last time-stamp tracking
        self.data_timestamp= m.timestamp
        return d

    def add_sample_timestamps(self,d:np.ndarray,timestamp:float,fs:float):
        """add per-sample timestamp information to the data matrix

        Args:
            d (np.ndarray): (t,d) the data matrix to attach time stamps to
            timestamp (float): the timestamp of the last sample of d
            fs (float): the nomional sample rate of d

        Returns:
            np.ndarray: (t,d+1) data matrix with attached time-stamp channel
        """
        if self.last_sample_timestamp is not None and self.last_sample_timestamp < timestamp:
            # update the tracker for the sample-number to sample timestamp mapping
            if self.sample2timestamp is not None:
                n=self.data_ringbuffer.n+len(d)
                #print("n={} ts={}".format(n,timestamp))
                newtimestamp = self.sample2timestamp.transform(n,timestamp)
                print("ts={} newts={} diff={}".format(timestamp,newtimestamp,timestamp-newtimestamp))
                # use the corrected de-jittered time-stamp -- if it's not tooo different
                if abs(timestamp-newtimestamp) < 50:
                    timestamp = int(newtimestamp)

            # simple linear interpolation for the sample time-stamps
            ts = np.linspace(self.last_sample_timestamp, timestamp, len(d)+1)
            ts = ts[1:]
        else:                
            if fs :
                # interpolate with the estimated sample rate                    
                ts = np.arange(-len(d)+1,1)*(1000/fs) + timestamp
            else:
                # give all same timestamp
                ts = np.ones(len(d))*timestamp

        # combine data with timestamps
        d = np.concatenate((np.array(d), ts[:, np.newaxis]), 1)
        self.last_sample_timestamp = timestamp
        return d

    def plot_raw_preproc_data(self, d_raw, d_preproc, ts):
        '''debugging function to check the diff between the raw and pre-processed data'''
        if not hasattr(self,'rawringbuffer'):
            self.preprocringbuffer=RingBuffer(maxsize=self.fs*3,shape=(d_preproc.shape[-1]+1,))
            self.rawringbuffer=RingBuffer(maxsize=self.raw_fs*3,shape=(d_raw.shape[-1]+1,))
        d_preproc = self.add_sample_timestamps(d_preproc,ts,self.fs)
        self.preprocringbuffer.extend(d_preproc)
        d_raw = self.add_sample_timestamps(d_raw,ts,self.raw_fs)
        self.rawringbuffer.extend(d_raw)
        if self.last_sigquality_ts is None or ts > self.last_sigquality_ts + self.send_sigquality_interval:
            import matplotlib.pyplot as plt
            plt.figure(10);plt.clf();
            idx = np.flatnonzero(self.rawringbuffer[:,-1])[0]
            plt.subplot(211); plt.cla(); plt.plot(self.rawringbuffer[idx:,-1],self.rawringbuffer[idx:,:-1])
            idx = np.flatnonzero(self.preprocringbuffer[:,-1])[0]
            plt.subplot(212); plt.cla(); plt.plot(self.preprocringbuffer[idx:,-1],self.preprocringbuffer[idx:,:-1])
            plt.show(block=False)


    def processStimulusEvent(self, m: StimulusEvent):
        '''pre-process a StimulusEvent message ready to be inserted into the stimulus ringbuffer'''
        # get the vector to hold the stimulus info
        d = np.zeros((257,),dtype=np.float32)

        if self.stimulus_ringbuffer is not None and self.stimulus_timestamp is not None:
            # hold value of used objIDs from previous time stamp
            d[:] = self.stimulus_ringbuffer[-1,:]

        # insert the  updated state
        d[m.objIDs] = m.objState
        d[-1] = m.timestamp
        # apply the pre-processor, if one was given
        if self.stimulus_preprocessor:
            d = self.stimulus_preprocessor.transform(d)

        # update the last time-stamp tracking
        self.stimulus_timestamp= m.timestamp
        return d

    def update_and_send_ElectrodeQualities(self, d_raw: np.ndarray, d_preproc: np.ndarray, ts: int):
        ''' compute running estimate of electrode qality and stream it '''
        raw_power, preproc_power = self.update_electrode_powers(d_raw, d_preproc)

        # convert to average amplitude
        raw_amp = np.sqrt(raw_power)
        preproc_amp = np.sqrt(preproc_power)

        # noise2signal estimated as removed raw amplitude (assumed=noise) to preprocessed amplitude (assumed=signal)
        noise2sig = np.maximum(float(1e-6), np.abs(raw_amp - preproc_amp)) /  np.maximum(float(1e-8),preproc_amp)

        # hack - detect disconnected channels
        noise2sig[ raw_power < 1e-6 ] = 100

        # hack - detect filter artifacts = preproc power is too big..
        noise2sig[ preproc_amp > raw_amp*10 ] = 100

        # hack - cap to 100
        noise2sig = np.minimum(noise2sig,100)

        # rate limit sending of signal-quality messages
        if self.last_sigquality_ts is None or ts > self.last_sigquality_ts + self.send_sigquality_interval:
            print("SigQ:\nraw_power=({}/{})\npp_power=({}/{})\nnoise2sig={}".format(
                   raw_amp,d_raw.shape[0],
                   preproc_amp,d_preproc.shape[0],
                   noise2sig))
            print("Q",end='')
            self.sendMessage(SignalQuality(ts, noise2sig))
            self.last_sigquality_ts = ts

            if self.VERBOSITY>2:
                # plot the sample time-stamp jitter...
                import matplotlib.pyplot as plt
                plt.figure(10)
                ts = self.data_ringbuffer[:,-1]
                idx = np.flatnonzero(ts)
                if len(idx)>0:
                    ts = ts[idx[0]:]
                    plt.subplot(211); plt.cla(); plt.plot(np.diff(ts)); plt.title('diff time-sample')
                    plt.subplot(212); plt.cla(); plt.plot((ts-ts[0])-np.arange(len(ts))*1000.0/self.fs); plt.title('regression against sample-number')
                    plt.show(block=False)

    def update_electrode_powers(self, d_raw: np.ndarray, d_preproc:np.ndarray):
        ''' track exp-weighted-moving average centered power for 2 input streams '''
        if self.raw_power is None:
            self.raw_power = power_tracker(self.raw_fs/10, self.raw_fs, self.raw_fs)
            self.preproc_power = power_tracker(self.fs/10, self.fs, self.fs)
        self.raw_power.transform(d_raw)
        self.preproc_power.transform(d_preproc)
        return (self.raw_power.power(), self.preproc_power.power())


    def update(self, timeout_ms=None, mintime_ms=None):
        '''Update the tracking state w.r.t. the utopia-hub.

        By adding data to the data_ringbuffer and (non-data) messages
        to the messages ring buffer.

        Args
         timeout_ms : int
             max block waiting for messages before returning
         mintime_ms : int
             min time to accumulate messages before returning
        Returns
          newmsgs : [newMsgs :UtopiaMessage]
             list of the *new* utopia messages from the server
          nsamp: int
             number of new data samples in this call
             Note: use data_ringbuffer[-nsamp:,...] to get the new data
          nstimulus : int
             number of new stimulus events in this call
             Note: use stimulus_ringbuffer[-nstimulus:,...] to get the new data
        '''
        if timeout_ms is None:
            timeout_ms = self.timeout_ms
        if mintime_ms is None:
            mintime_ms = self.mintime_ms
        nsamp = 0
        nmsg = 0
        nstimulus = 0
        if not self.isConnected():
            self.connect()
        if not self.isConnected():
            return [],0,0
        if self.data_ringbuffer is None: # do special init stuff if not done
            nsamp, nmsg = self.initDataRingBuffer()
        if self.stimulus_ringbuffer is None: # do special init stuff if not done
            self.initStimulusRingBuffer()
        if self.last_log_ts is None:
            self.last_log_ts = self.getTimeStamp()
        t0 = self.getTimeStamp()

        # record the list of new messages from this call
        newmsgs = self.newmsgs # start with any left-overs from old calls 
        self.newmsgs=[] # clear the  left-over messages stack
        
        ttg = timeout_ms - (self.getTimeStamp() - t0) # time-to-go in the update loop
        while ttg > 0:

            # rate limit
            if ttg >= mintime_ms:
                sleep(mintime_ms/1000.0)
                ttg = timeout_ms - (self.getTimeStamp() - t0) # udate time-to-go
                
            # get the new messages
            msgs = self.getNewMessages(ttg)

            # process the messages - basically to split datapackets from the rest
            print(".",end='')
            #print("{} in {}".format(len(msgs),self.getTimeStamp()-t0),end='',flush=True)
            for m in msgs:
                m = self.preprocess_message(m)
                
                print("{:c}".format(m.msgID), end='', flush=True)
                
                if m.msgID == DataPacket.msgID: # data-packets are special
                    d = self.processDataPacket(m) # (samp x ...)
                    self.data_ringbuffer.extend(d)
                    nsamp = nsamp + d.shape[0]
                    
                elif m.msgID == StimulusEvent.msgID: # as are stmiuluse events
                    d = self.processStimulusEvent(m) # (nY x ...)
                    self.stimulus_ringbuffer.append(d)
                    nstimulus = nstimulus + 1
                    
                else:
                    # NewTarget/Selection are also special in that they clear stimulus state...
                    if m.msgID == NewTarget.msgID or m.msgID == Selection.msgID :
                        # Make a dummy stim-event to reset all objIDs to off
                        d = self.processStimulusEvent(StimulusEvent(m.timestamp,
                                                                    np.arange(255,dtype=np.int32),
                                                                    np.zeros(255,dtype=np.int8)))
                        self.stimulus_ringbuffer.append(d)
                        self.stimulus_timestamp= m.timestamp
                    
                    if len(self.msg_ringbuffer)>0 and m.timestamp > self.msg_ringbuffer[0].timestamp + self.msgwindow_ms: # slide msg buffer
                        self.msg_ringbuffer.popleft()
                    self.msg_ringbuffer.append(m)
                    newmsgs.append(m)
                    nmsg = nmsg+1
                    self.msg_timestamp = m.timestamp
                
            # update time-to-go
            ttg = timeout_ms - (self.getTimeStamp() - t0)

        # new line
        if self.getTimeStamp() > self.last_log_ts + 2000:
            print("",flush=True)
            self.last_log_ts = self.getTimeStamp()
            
        # return new mesages, and count new samples/stimulus 
        return (newmsgs, nsamp, nstimulus)

    def push_back_newmsgs(self,oldmsgs):
        '''put unprocessed messages back onto the  newmessages queue'''
        # TODO []: ensure  this preserves message time-stamp order?
        self.newmsgs.extend(oldmsgs)

    def extract_data_segment(self, bgn_ts, end_ts=None):
        return extract_ringbuffer_segment(self.data_ringbuffer,bgn_ts,end_ts)
    
    def extract_stimulus_segment(self, bgn_ts, end_ts=None):
        return extract_ringbuffer_segment(self.stimulus_ringbuffer,bgn_ts,end_ts)
    
    def extract_msgs_segment(self, bgn_ts, end_ts=None):
        ''' extract the messages between start/end time stamps'''
        msgs = [] # store the trial stimEvents
        for m in reversed(self.msg_ringbuffer):
            if m.timestamp <= bgn_ts:
                # stop as soon as earlier than bgn_ts
                break
            if end_ts is None or m.timestamp < end_ts:
                msgs.append(m)
        # reverse back to input order
        msgs.reverse()
        return msgs

    def run(self, timeout_ms=30000):
        '''test run the interface forever, just getting and storing data'''
        t0 = self.getTimeStamp()
        # test getting 5s data
        tstart = self.data_timestamp
        trlen_ms = 5000
        while self.getTimeStamp() < t0+timeout_ms:
            self.update()
            # test getting a data segment
            if tstart is None :
                tstart = self.data_timestamp
            if tstart and self.data_timestamp > tstart + trlen_ms:
                X = self.extract_data_segment(tstart, tstart+trlen_ms)
                print("Got data: {}->{}\n{}".format(tstart, tstart+trlen_ms, X[:, -1]))
                Y = self.extract_stimulus_segment(tstart, tstart+trlen_ms)
                print("Got stimulus: {}->{}\n{}".format(tstart, tstart+trlen_ms, Y[:, -1]))
                tstart = self.data_timestamp + 5000
            print('.', flush=True)


try:
    from sklearn.base import TransformerMixin
except:
    # fake the class if sklearn is not available, e.g. Android/iOS
    class TransformerMixin:
        def __init__():
            pass
        def fit(self,X):
            pass
        def transform(self,X):
            pass

from mindaffectBCI.decoder.utils import sosfilt, butter_sosfilt, sosfilt_zi_warmup
class butterfilt_and_downsample(TransformerMixin):
    def __init__(self, stopband=((0,5),(5,-1)), order:int=6, fs:float =250, fs_out:float =60):
        self.stopband = stopband
        self.fs = fs
        self.fs_out = fs_out
        self.order = order
        self.axis = -2
        self.nsamp = 0

    def fit(self, X, fs:float =None, zi=None):
        if fs is not None: # parameter overrides stored fs
            self.fs = fs

        # preprocess -> spectral filter
        if isinstance(self.stopband, str):
            import pickle
            import os
            # load coefficients from file -- when scipy isn't available
            if os.path.isfile(self.stopband):
                fn = self.stopband 
            else: # try relative to our py file
                fn = os.path.join(os.path.dirname(os.path.abspath(__file__)),self.stopband)
            with open(fn,'rb') as f:
                self.sos_ = pickle.load(f)
                self.zi_ = pickle.load(f)
                f.close()
            # tweak the shape/scale of zi to the actual data shape
            self.zi_ = sosfilt_zi_warmup(self.zi_, X, self.axis)
            print("X={} zi={}".format(X.shape,self.zi_.shape))

        else:
            # estimate them from the given information
            X, self.sos_, self.zi_ = butter_sosfilt(X, self.stopband, self.fs, self.order, axis=self.axis, zi=zi)
            
        # preprocess -> downsample
        self.nsamp = 0
        self.resamprate_ = int(round(self.fs*2.0/self.fs_out))/2.0
        print("resample: {}->{}hz rsrate={}".format(self.fs, self.fs/self.resamprate_, self.resamprate_))

        return self

    def transform(self, X, Y=None):
        # propogate the filter coefficients between calls
        X, self.zi_ = sosfilt(self.sos_, X, axis=self.axis, zi=self.zi_)

        # preprocess -> downsample @60hz
        if self.resamprate_ > 1:
            # number samples through this cycle due to remainder of last block
            resamp_start = self.nsamp%self.resamprate_
            # convert to number samples needed to complete this cycle
            # this is then the sample to take for the next cycle
            if resamp_start > 0:
                resamp_start = self.resamprate_ - resamp_start
            
            # allow non-integer resample rates
            idx =  np.arange(resamp_start,X.shape[self.axis],self.resamprate_).astype(np.int)
            #print('idx={}'.format(self.nsamp+idx))

            self.nsamp = self.nsamp + X.shape[self.axis] # track sample counter
            X = X[..., idx, :] # decimate X (trl, samp, d)
            if Y is not None:
                Y = Y[..., idx, :] # decimate Y (trl, samp, y)
        else:
            self.nsamp = self.nsamp + X.shape[self.axis] # track sample counter
        
        return X if Y is None else (X, Y)

    @staticmethod
    def testcase():
        ''' test the filt+downsample transformation filter by incremental calling '''
        X=np.cumsum(np.random.randn(100,1),axis=0)
        # high-pass and decimate
        fds = butterfilt_and_downsample(stopband=((0,1)),fs=200,fs_out=80)

        
        print("single step")
        fds.fit(X[0:1,:])
        m0 = fds.transform(X) # (samp,ny,ne)
        print("M0 -> {}".format(m0[:20]))

        step=6
        print("Step size = {}".format(step))
        fds.fit(X[0:0+step,:])
        m1=np.zeros(m0.shape,m0.dtype)
        t=0
        for i in range(0,len(X),step):
            idx=slice(i,i+step)
            mm=fds.transform(X[idx,:])
            #m1[t:t+mm.shape[0],:]=mm
            t = t +mm.shape[0]
        print("M1 -> {}".format(m1[:20]))
        print("diff: {}".format(np.max(np.abs(m0-m1))))


from mindaffectBCI.decoder.stim2event import stim2event
class stim2eventfilt(TransformerMixin):
    ''' transformer to transform a sequence of stimulus states to a brain event sequence '''
    def __init__(self, evtlabs=None, histlen=20):
        self.evtlabs = evtlabs
        self.histlen = histlen
        self.prevX = None

    def fit(self, X):
        return self

    def transform(self, X):
        '''transform Stimulus-encoded to brain-encoded'''
        if X is None:
            return None
        
        # keep old fitler state for the later transformation call
        prevX = self.prevX

        # grab the new filter state (if wanted)
        if self.histlen>0:
            #print('prevX={}'.format(prevX))
            #print("X={}".format(X))
            if X.shape[0] >= self.histlen or prevX is None:
                self.prevX = X
            else:
                self.prevX = np.append(prevX, X, 0)
            # only keep the last bit -- copy in case gets changed in-place
            self.prevX = self.prevX[-self.histlen:,:].copy()
            #print('new_prevX={}'.format(self.prevX))

        # convert from stimulus coding to brain response coding, with old state
        X = stim2event(X, self.evtlabs, axis=-2, oM=prevX)
        return X

    def testcase():
        ''' test the stimulus transformation filter by incremental calling '''
        M=np.array([0,0,0,1,0,0,1,1,0,1])[:,np.newaxis] # samp,nY
        s2ef = stim2eventfilt(evtlabs=('re','fe'),histlen=3)

        print("single step")
        m0=s2ef.transform(M) # (samp,ny,ne)
        print("{} -> {}".format(M,m0))

        print("Step size = 1")
        m1=np.zeros(m0.shape,m0.dtype)
        for i in range(len(M)):
            idx=slice(i,i+1)
            mm=s2ef.transform(M[idx,:])
            m1[idx,...]=mm
            print("{} {} -> {}".format(i,M[idx,...],mm))

        print("Step size=4")
        m4=np.zeros(m0.shape,m0.dtype)
        for i in range(0,len(M),4):
            idx=slice(i,i+4)
            mm=s2ef.transform(M[idx,:])
            m4[idx,...]=mm
            print("{} {} -> {}".format(i,M[idx,...],mm))

        print("m0={}\nm1={}\n,m4={}\n".format(m0,m1,m4))
            

class power_tracker():
    def __init__(self,halflife_mu_ms, halflife_power_ms, fs):
        # convert to per-sample decay factor
        self.alpha_mu = self.hl2alpha(fs * halflife_mu_ms / 1000.0 ) 
        self.alpha_power= self.hl2alpha(fs * halflife_power_ms / 1000.0 )
        self.sX_N = None
        self.sX = None
        self.sXX_N = None
        self.sXX = None

    def hl2alpha(self,hl):
        return np.exp(np.log(.5)/hl)

    def fit(self,X):
        self.sX_N = X.shape[0]
        self.sX = np.sum(X,axis=0)
        self.sXX_N = X.shape[0]
        self.sXX = np.sum((X-(self.sX/self.sX_N))**2,axis=0)
        return self.power()

    def transform(self, X: np.ndarray):
        ''' compute the exponientially weighted centered power of X '''
        if self.sX is None: # not fitted yet!
            return self.fit(X)
        # compute updated mean
        alpha_mu   = self.alpha_mu ** X.shape[0]
        self.sX_N  = self.sX_N*alpha_mu + X.shape[0]
        self.sX    = self.sX*alpha_mu + np.sum(X, axis=0)
        # center and compute updated power
        alpha_pow  = self.alpha_power ** X.shape[0]
        self.sXX_N = self.sXX_N*alpha_pow + X.shape[0]
        self.sXX   = self.sXX*alpha_pow + np.sum((X-(self.sX/self.sX_N))**2, axis=0)       
        return self.power()
    
    def mean(self):
        return self.sX / self.sX_N
    def power(self):
        return self.sXX / self.sXX_N
    
    def testcase(self):
        X = np.random.randn(10000,2)
        #X = np.cumsum(X,axis=0)
        pt = power_tracker(100,100,100)
        print("All at once: power={}".format(pt.transform(X)))  # all at once
        pt = power_tracker(100,1000,1000)
        print("alpha_mu={} alpha_pow={}".format(pt.alpha_mu,pt.alpha_power) )
        step = 30
        idxs = list(range(step,X.shape[0],step))
        powers = np.zeros((len(idxs),X.shape[-1]))
        mus = np.zeros((len(idxs),X.shape[-1]))
        for i,j in enumerate(idxs):
            powers[i,:] = np.sqrt(pt.transform(X[j-step:j,:]))
            mus[i,:]=pt.mean()
        for d in range(X.shape[-1]):
            plt.subplot(X.shape[-1],1,d+1)
            plt.plot(X[:,d])
            plt.plot(idxs,mus[:,d])
            plt.plot(idxs,powers[:,d])

def testfilt():
    butterfilt_and_downsample.testcase()

def testRaw():
    # test with raw
    ui = UtopiaDataInterface()
    ui.connect()
    sigViewer(ui,30000) # 30s sigviewer

def testPP():
    from sigViewer import sigViewer
    # test with a filter + downsampler
    ppfn= butterfilt_and_downsample(order=4, stopband=((0,1),(25,-1)), fs_out=60)
    #ppfn= butterfilt_and_downsample(order=4, stopband='butter_stopband((0, 5), (25, -1))_fs200.pk', fs_out=80)     
    ui = UtopiaDataInterface(data_preprocessor=ppfn, stimulus_preprocessor=None)
    ui.connect()
    sigViewer(ui)

def testFileProxy(filename):
    from mindaffectBCI.decoder.FileProxyHub import FileProxyHub
    U = FileProxyHub(filename)
    fs = 200
    from sigViewer import sigViewer
    # test with a filter + downsampler
    ppfn= butterfilt_and_downsample(order=4, stopband=((0,3),(25,-1)), fs_out=200)
    ui = UtopiaDataInterface(data_preprocessor=ppfn, stimulus_preprocessor=None, mintime_ms=0, U=U, fs=fs)
    ui.connect()
    sigViewer(ui)

def testERP():
    ui = UtopiaDataInterface()
    ui.connect()
    erpViewer(ui,evtlabs=None) # 30s sigviewer

def testElectrodeQualities(X,fs=200,pktsize=20):
    # recurse if more dims than we want...
    if X.ndim>2:
        sigq=[]
        for i in range(X.shape[0]):
            sigqi = testElectrodeQualities(X[i,...],fs,pktsize)
            sigq.append(sigqi)
        sigq=np.concatenate(sigq,0)
        return sigq
    
    ppfn= butterfilt_and_downsample(order=6, stopband='butter_stopband((0, 5), (25, -1))_fs200.pk', fs_out=100)
    ppfn.fit(X[:10,:],fs=200)
    noise2sig = np.zeros((int(X.shape[0]/pktsize),X.shape[-1]),dtype=np.float32)
    for pkti in range(noise2sig.shape[0]):
        t = pkti*pktsize
        Xi = X[t:t+pktsize,:]
        Xip = ppfn.transform(Xi)
        raw_power, preproc_power = UtopiaDataInterface.update_electrode_powers(Xi,Xip)
        noise2sig[pkti,:] = np.maximum(float(1e-6), (raw_power - preproc_power)) /  np.maximum(float(1e-8),preproc_power)
    return noise2sig

    
if __name__ == "__main__":
    #testfilt()
    #testRaw()
    #testPP()
    #testERP()
    testFileProxy("..\..\Downloads\khash\mindaffectBCI_noisetag_bci_200907_1433.txt")
