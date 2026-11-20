// Modified by fortress-onenote (vendor/PATCHES.md: C3).
use std::io;

use byteorder::{LittleEndian, WriteBytesExt};


const MSZIP_SIGNATURE: u16 = 0x4B43; // "CK" stored little-endian
const MSZIP_SIGNATURE_LEN: usize = 2;
const DEFLATE_MAX_DICT_LEN: usize = 0x8000;

pub struct MsZipDecompressor {
    decompressor: flate2::Decompress,
    dictionary: Vec<u8>,
}

impl MsZipDecompressor {
    pub fn new() -> MsZipDecompressor {
        MsZipDecompressor {
            decompressor: flate2::Decompress::new(false),
            dictionary: Vec::with_capacity(DEFLATE_MAX_DICT_LEN),
        }
    }

    pub fn reset(&mut self) {
        self.decompressor.reset(true);
        self.dictionary = Vec::with_capacity(DEFLATE_MAX_DICT_LEN);
    }

    pub fn decompress_block(
        &mut self,
        data: &[u8],
        uncompressed_size: usize,
    ) -> io::Result<Vec<u8>> {
        // Check signature:
        if data.len() < MSZIP_SIGNATURE_LEN
            || ((data[0] as u16) | ((data[1] as u16) << 8)) != MSZIP_SIGNATURE
        {
            invalid_data!(
                "MSZIP decompression failed: Invalid block signature"
            );
        }
        let data = &data[MSZIP_SIGNATURE_LEN..];
        // Reset decompressor with appropriate dictionary:
        self.decompressor.reset(false);
        if !self.dictionary.is_empty() {
            // TODO: Avoid doing extra allocations/copies here.
            debug_assert!(self.dictionary.len() <= DEFLATE_MAX_DICT_LEN);
            let length = self.dictionary.len() as u16;
            let mut chunk: Vec<u8> = vec![0];
            chunk.write_u16::<LittleEndian>(length)?;
            chunk.write_u16::<LittleEndian>(!length)?;
            chunk.extend_from_slice(&self.dictionary);
            let mut out = Vec::with_capacity(self.dictionary.len());
            let flush = flate2::FlushDecompress::Sync;
            match self.decompressor.decompress_vec(&chunk, &mut out, flush) {
                Ok(flate2::Status::Ok) => {}
                _ => unreachable!(),
            }
        }
        // Decompress data:
        let mut out = Vec::<u8>::with_capacity(uncompressed_size);
        let flush = flate2::FlushDecompress::Finish;
        match self.decompressor.decompress_vec(data, &mut out, flush) {
            Ok(_) => {}
            Err(error) => {
                invalid_data!("MSZIP decompression failed: {}", error);
            }
        }
        if out.len() != uncompressed_size {
            invalid_data!(
                "MSZIP decompression failed: Incorrect uncompressed size \
                 (expected {}, was actually {})",
                uncompressed_size,
                out.len()
            );
        }
        // Update dictionary for next block:
        if out.len() >= DEFLATE_MAX_DICT_LEN {
            let start = out.len() - DEFLATE_MAX_DICT_LEN;
            self.dictionary = out[start..].to_vec();
        } else {
            let total = self.dictionary.len() + out.len();
            if total > DEFLATE_MAX_DICT_LEN {
                self.dictionary.drain(..(total - DEFLATE_MAX_DICT_LEN));
            }
            self.dictionary.extend_from_slice(&out);
        }
        debug_assert_eq!(self.dictionary.capacity(), DEFLATE_MAX_DICT_LEN);
        Ok(out)
    }
}
